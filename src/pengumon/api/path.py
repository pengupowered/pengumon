"""The probe path for the path editor (engine/path.py has the model): read it, apply an edited one, test a hop,
delete a removed hop's history, export it as YAML and read an export back as a draft.

GET /api/path               {"revision", "hops": [hop...], "managed_by": "dashboard"}; revision 0: never applied
PUT /api/path               {"base_revision", "hops"} -> the new path; 409 when the path changed since base_revision
DELETE /api/path/history/{id}   the stored probes of a hop removed from the path
POST /api/path/test         {"address", "family"} -> one probe: {"kind", "rtt_ms", "responder", "address", "error"}
GET /api/path/export        the path as YAML (a targets: list in config.yaml syntax)
POST /api/path/import       that YAML -> {"hops": [...]}, a draft: nothing is applied
"""

import asyncio
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from pengumon.api.common import StateDep
from pengumon.db.path import PathConflict
from pengumon.engine.icmp_base import IcmpUnavailable
from pengumon.engine.icmp_packet import ip_literal
from pengumon.engine.icmp_select import resolve_address
from pengumon.engine.netinfo import is_no_ipv6_error, needs_route_check
from pengumon.engine.path import PathError, PathManager, PathUpdate, hop_dict, path_manager
from pengumon.models.hosts import validate_host
from pengumon.models.probes import TargetConfig
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")

_EDIT_RATE = rate_limit("path", 20, 60)  # applying a path and deleting a removed hop's history share one bucket


def _manager(state) -> PathManager:
    manager = path_manager(state)
    if manager is None:
        raise HTTPException(status_code=503, detail="Engine not ready")
    return manager


def _path_body(revision: int, hops: List[TargetConfig]) -> Dict[str, Any]:
    return {"revision": revision, "hops": [hop_dict(t, i) for i, t in enumerate(hops)], "managed_by": "dashboard"}


@router.get("/path")
async def get_path(state: StateDep):
    """The probe path as it is live, in path order (position 0 nearest to this host)."""
    return _path_body(*await _manager(state).current())


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
    prober = state.prober

    def failed(error: str, address: Optional[str] = None, kind: str = "error"):
        return {"kind": kind, "rtt_ms": None, "responder": None, "address": address, "error": error}

    def no_ipv6(address: Optional[str] = None):
        return failed("IPv6 unavailable on this host", address, "ipv6_unavailable")

    try:
        backend = prober.backend()
    except IcmpUnavailable as e:
        return failed(f"ICMP unavailable: {e.reason}")
    try:
        literal = ip_literal(req.address)[1]
    except ValueError:
        literal = None
    if (literal or req.family) == 6 and needs_route_check(req.address) and not prober.ipv6.available():
        return no_ipv6()
    try:
        address, version = await resolve_address(req.address, prober.ping_timeout, req.family)
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


IMPORT_MAX_BYTES = 256 * 1024
IMPORT_MAX_DEPTH = 16  # nesting of the YAML document; an export is three deep


def check_yaml_plain(text: str) -> None:
    """Refuses what an export never contains and what can blow up while loading: anchors, aliases and merge keys
    (a few hundred bytes of nested aliases expand to gigabytes) and deep nesting. Works on the parser's events, so
    nothing is constructed. Raises ValueError; a YAML syntax error is left to the loader."""
    depth = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ValueError("Anchors and aliases (&name, *name) are not allowed in a path file")
            if isinstance(event, yaml.ScalarEvent) and event.value == "<<" and event.implicit[0]:
                raise ValueError("Merge keys (<<) are not allowed in a path file")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > IMPORT_MAX_DEPTH:
                    raise ValueError("That file is nested too deeply to be a path")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
    except yaml.YAMLError:
        return  # reported by the loader with its position


def read_import(text: str) -> List[TargetConfig]:
    """The hops of an export (or of a config.yaml: its targets: section; a bare list works too), validated like a
    PUT. Raises ValueError with a plain message; a YAML error is reported by position only."""
    if len(text) > IMPORT_MAX_BYTES:
        raise ValueError("That file is too big to be a path")
    check_yaml_plain(text)
    try:
        data = yaml.safe_load(text)
    except RecursionError:
        raise ValueError("That file is nested too deeply to be a path") from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ValueError(f"That is not valid YAML{where}: {getattr(e, 'problem', None) or type(e).__name__}") from None
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
    return {"hops": [hop_dict(t, i) for i, t in enumerate(targets)]}
