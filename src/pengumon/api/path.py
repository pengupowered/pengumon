"""The probe path for the path editor (engine/path.py has the model): read it, apply an edited one, test a hop,
delete a removed hop's history, export it as YAML and read an export back as a draft.

GET /api/path               {"revision", "hops": [hop...], "managed_by": "dashboard", "device_matches": {hop id: device id}};
                            revision 0: never applied; device_matches: the home hops without a device that one
                            configured device's address matches (engine.device_links), which the editor links
PUT /api/path               {"base_revision", "hops"} -> the new path; 409 when the path changed since base_revision
DELETE /api/path/history/{id}   the stored probes of a hop removed from the path
POST /api/path/test         {"address", "family"} -> one probe: {"kind", "rtt_ms", "responder", "address", "error"}
GET /api/path/export        the path as YAML (a targets: list in config.yaml syntax)
POST /api/path/import       that YAML -> {"hops": [...], "device_matches"}, a draft: nothing is applied
POST /api/path/preview      {"hops"} (a draft, as PUT takes it) -> {"id", "probed"}: its new hops probed for 30 s
GET /api/path/preview/{id}  {"state", "elapsed_s", "hops": {id: {"sent", "received", "loss_pct", "median_ms",
                            "last_ms", "kind"}}};  DELETE /api/path/preview/{id} stops it
GET /api/path/versions      the applied versions, newest first: [{"revision", "applied_at", "hop_count", "summary"}]
GET /api/path/versions/{n}  {"revision", "hops"}: that version as a draft (the editor applies it to restore it)
GET /api/path/events        the path's events ("address_changed"), oldest first; ?target_id= for one hop's
GET /api/path/drift         the latest background route-change check; POST /api/path/drift/dismiss: seen
"""

import asyncio
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from pengumon.api.common import StateDep
from pengumon.db.path import PathConflict
from pengumon.engine.icmp_base import IcmpUnavailable
from pengumon.engine.icmp_packet import ip_literal
from pengumon.engine.icmp_select import resolve_address
from pengumon.engine.netinfo import is_no_ipv6_error, needs_route_check
from pengumon.engine.device_links import device_matches
from pengumon.engine.discovery.draft import address_key
from pengumon.engine.path import PathDraft, PathError, PathManager, PathUpdate, hop_dict, path_manager
from pengumon.engine.path_preview import PreviewBusy
from pengumon.models.hosts import validate_host
from pengumon.models.probes import TargetConfig
from pengumon.security import rate_limit
from pengumon.yamlsafe import IMPORT_MAX_BYTES, IMPORT_MAX_DEPTH, check_yaml_plain, load_plain  # noqa: F401

router = APIRouter(prefix="/api")

_EDIT_RATE = rate_limit("path", 20, 60)  # applying a path and deleting a removed hop's history share one bucket


def _manager(state) -> PathManager:
    manager = path_manager(state)
    if manager is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    return manager


def _path_body(revision: int, hops: List[TargetConfig]) -> Dict[str, Any]:
    return {"revision": revision, "hops": [hop_dict(t, i) for i, t in enumerate(hops)], "managed_by": "dashboard"}


async def _matches(manager: PathManager, hops: List[TargetConfig]) -> Dict[str, str]:
    extra = {"resolve": manager._resolve} if manager._resolve else {}
    return await device_matches(hops, manager.config.devices, **extra)


@router.get("/path")
async def get_path(state: StateDep):
    """The probe path as it is live, in path order (position 0 nearest to this host), and the device each home hop
    without one would be linked to."""
    manager = _manager(state)
    revision, hops = await manager.current()
    return {**_path_body(revision, hops), "device_matches": await _matches(manager, hops)}


@router.put("/path", dependencies=[Depends(_EDIT_RATE)])
async def put_path(update: PathUpdate, state: StateDep):
    """Applies an edited path at once (one transaction; the prober and the classifier follow without a restart).
    Hops left out stop being probed; their history is kept, hidden."""
    manager = _manager(state)
    targets = update.targets()
    try:
        revision = await manager.apply(update.base_revision, targets)
    except PathConflict as e:
        return JSONResponse(status_code=409, content={
            "detail": "The path was changed elsewhere since this edit started; reload it and edit again.",
            "revision": e.current})
    except PathError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return _path_body(revision, targets)


@router.delete("/path/history/{target_id}", dependencies=[Depends(_EDIT_RATE)])
async def delete_history(target_id: str, state: StateDep):
    """Deletes the stored probes of a hop that was removed from the path (404 for any other id: the history of a hop
    on the path is never deleted)."""
    manager = _manager(state)
    if target_id in {d.id for d in manager.config.dns_probes}:
        raise HTTPException(status_code=400, detail="That is a DNS lookup, not a removed hop")
    async with manager.lock:  # an apply cannot bring the hop back between the check and the delete
        if any(t.id == target_id for t in manager.config.targets):
            raise HTTPException(status_code=400, detail="That hop is on the path; remove it and apply first")
        if not await manager.repository.delete_path_history(target_id):
            raise HTTPException(status_code=404, detail="No removed hop with that id")
    return {"message": f"History of {target_id} deleted"}


class PathTestRequest(BaseModel):
    address: str = Field(..., max_length=255)
    family: Literal[4, 6] = 4  # for a host name: probe its IPv4 or IPv6 address (a literal is probed as it is)

    @field_validator("address")
    @classmethod
    def _host(cls, v: str) -> str:
        return validate_host(v)


@router.post("/path/test", dependencies=[Depends(rate_limit("path_test", 30, 60))])
async def test_hop(req: PathTestRequest, state: StateDep):
    """One echo request to the address through the app's ICMP backend, as the prober sends it. An IPv6 address while
    this host has no IPv6 is kind "ipv6_unavailable" (nothing is sent), as the prober treats such a target."""
    if not state.prober:
        raise HTTPException(status_code=503, detail="Prober engine initializing")
    return await probe_address(state.prober, req.address, req.family)


async def probe_address(prober, address_in: str, family: int) -> Dict[str, Any]:
    """One echo request to `address_in` (a literal, or a name looked up in `family`) through the prober's backend:
    {"kind", "rtt_ms", "responder", "address", "error"}. Records nothing."""

    def failed(error: str, address: Optional[str] = None, kind: str = "error"):
        return {"kind": kind, "rtt_ms": None, "responder": None, "address": address, "error": error}

    def no_ipv6(address: Optional[str] = None):
        return failed("IPv6 unavailable on this host", address, "ipv6_unavailable")

    try:
        backend = prober.backend()
    except IcmpUnavailable as e:
        return failed(f"ICMP unavailable: {e.reason}")
    try:
        literal = ip_literal(address_in)[1]
    except ValueError:
        literal = None
    if (literal or family) == 6 and needs_route_check(address_in) and not prober.ipv6.available():
        return no_ipv6()
    try:
        address, version = await resolve_address(address_in, prober.ping_timeout, family)
    except ValueError as e:
        return failed(str(e))
    except OSError as e:
        return failed(f"Name lookup failed: {e}")
    if version == 6 and literal is None and needs_route_check(address) and not prober.ipv6.available():
        return no_ipv6(address)
    try:
        result = await backend.probe(address, timeout=prober.ping_timeout)
    except OSError as e:
        if version == 6 and is_no_ipv6_error(e):
            prober.ipv6.no_route(e)
            return no_ipv6(address)
        return failed(f"ICMP send failed: {e}", address)
    rtt = round(result.rtt_ms, 2) if result.rtt_ms is not None else None
    return {"kind": result.kind, "rtt_ms": rtt, "responder": result.responder, "address": address, "error": None}


# --- preview before apply --------------------------------------------------------------------------------------------

def preview_hops(draft: List[TargetConfig], live: List[TargetConfig]) -> Dict[str, tuple]:
    """The draft hops a preview probes: enabled, and at an address (and family) no live hop is probed at already
    (a new hop, or one whose address changed). {hop id: (address, family)}."""
    probed = {(address_key(t.address), t.family) for t in live if t.enabled}
    return {t.id: (t.address, t.family) for t in draft
            if t.enabled and (address_key(t.address), t.family) not in probed}


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.post("/path/preview", dependencies=[Depends(rate_limit("path_preview", 12, 60))])
async def start_preview(draft: PathDraft, request: Request, state: StateDep):
    """Probes the draft's hops that are not probed live yet, once a second for 30 s, recording nothing. A client's
    new preview stops its previous one; 429 while two others run."""
    manager = _manager(state)
    if not state.prober:
        raise HTTPException(status_code=503, detail="Prober engine initializing")
    hops = preview_hops(draft.targets(), list(manager.config.targets))
    prober = state.prober
    try:
        preview_id = await manager.previews.start(
            _client(request), hops, lambda address, family: probe_address(prober, address, family))
    except PreviewBusy:
        raise HTTPException(status_code=429, detail="Two previews are running already; try again in half a minute.",
                            headers={"Retry-After": "30"}) from None
    return {"id": preview_id, "probed": list(hops)}


@router.get("/path/preview/{preview_id}")
async def get_preview(state: StateDep, preview_id: str = Path(..., max_length=64)):
    body = _manager(state).previews.get(preview_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Unknown preview")
    return body


@router.delete("/path/preview/{preview_id}")
async def stop_preview(state: StateDep, preview_id: str = Path(..., max_length=64)):
    previews = _manager(state).previews
    if not await previews.stop(preview_id):
        raise HTTPException(status_code=404, detail="Unknown preview")
    return previews.get(preview_id)


# --- versions and events ---------------------------------------------------------------------------------------------

@router.get("/path/versions")
async def list_versions(state: StateDep):
    """Every kept applied version, newest first (the last 50; revision 1 is the import)."""
    return await _manager(state).repository.path_versions()


@router.get("/path/versions/{revision}")
async def get_version(state: StateDep, revision: int = Path(..., ge=0)):
    """A version's hops as a draft for the editor: applying it restores that path (a hop keeps its id, and with it its
    history). A link to a device that is no longer configured is dropped, as an import drops it."""
    manager = _manager(state)
    hops = await manager.repository.path_version(revision)
    if hops is None:
        raise HTTPException(status_code=404, detail="No such version (the last 50 are kept)")
    devices = {d.id for d in manager.config.devices}
    out = []
    for hop in hops:
        hop = dict(hop)
        if hop.get("device_id") not in devices:
            hop["device_id"] = None
        out.append(hop)
    return {"revision": revision, "hops": out}


@router.get("/path/events")
async def list_events(state: StateDep, target_id: Optional[str] = Query(None, max_length=64)):
    """What happened to the path's hops, oldest first: {"at", "kind": "address_changed", "target_id", "old", "new",
    "revision"}. The target history dialog marks them on its charts."""
    return await _manager(state).repository.path_events(target_id)


# --- route-change checks ---------------------------------------------------------------------------------------------

NEVER = {"checked_at": None, "state": "never", "changes": [], "discovery_id": None, "error": None}


@router.get("/path/drift")
async def get_drift(state: StateDep):
    """The latest background route-change check, compared with the path as it is applied now (state "never" while
    none ran, or with discovery.background off)."""
    if state.drift is None:
        return dict(NEVER)
    return await state.drift.public()


@router.post("/path/drift/dismiss", dependencies=[Depends(rate_limit("path_drift", 20, 60))])
async def dismiss_drift(state: StateDep):
    """Marks the changes shown now as seen: they are not shown or announced again while later checks see them."""
    if state.drift is None:
        return dict(NEVER)
    return await state.drift.dismiss()


def export_yaml(targets: List[TargetConfig]) -> str:
    """The hops as config.yaml's targets: list, leaving out the fields at their defaults."""
    keep = ("id", "name", "category", "address")
    rows = []
    for t in targets:
        values = t.model_dump(exclude_defaults=True)
        rows.append({**{k: getattr(t, k) for k in keep}, **{k: v for k, v in values.items() if k not in keep}})
    header = "# Pengumon probe path (Path health > Edit > Export). Import it in the path editor of any install.\n"
    return header + yaml.safe_dump({"targets": rows}, sort_keys=False, allow_unicode=True)


@router.get("/path/export")
async def export_path(state: StateDep):
    _, targets = await _manager(state).current()
    return Response(export_yaml(targets), media_type="application/yaml",
                    headers={"Content-Disposition": 'attachment; filename="pengumon-path.yaml"'})


def read_import(text: str) -> List[TargetConfig]:
    """The hops of an export (or of a config.yaml: its targets: section; a bare list works too), validated like a
    PUT. Raises ValueError with a plain message; a YAML error is reported by position only."""
    data = load_plain(text, "path file")
    if isinstance(data, dict):
        data = data.get("targets")
    if not isinstance(data, list) or not data:
        raise ValueError("No targets found: the file needs a targets: list (as Export writes it)")
    if not all(isinstance(row, dict) for row in data):
        raise ValueError("Every entry of targets: must be a mapping (id, name, category, address, ...)")
    update = PathUpdate(base_revision=0, hops=[{**row, "position": i} for i, row in enumerate(data)])
    return update.targets()


@router.post("/path/import", dependencies=[Depends(rate_limit("path_import", 20, 60))])
async def import_path(request: Request, state: StateDep):
    """Reads an exported path (YAML in the body) into a draft for the editor. Nothing is applied."""
    manager = _manager(state)
    try:
        text = (await request.body()).decode("utf-8", errors="replace")
        targets = await asyncio.to_thread(read_import, text)
        devices = {d.id for d in manager.config.devices}
        for t in targets:  # a device of the install the file came from: not linked here
            if t.device_id not in devices:
                t.device_id = None
        manager.check(targets)
    except ValidationError as e:
        detail = e.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(status_code=422, detail=jsonable_encoder(detail)) from None
    except (ValueError, PathError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"hops": [hop_dict(t, i) for i, t in enumerate(targets)], "device_matches": await _matches(manager, targets)}
