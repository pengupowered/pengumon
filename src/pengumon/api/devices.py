"""Device and feature endpoints: /api/devices (list, status, credentials, history) and /api/features.

The dashboard reads the device list and the features first, then talks to each device by its id.

Credentials are written to data/secrets.json (the app's secrets.SecretStore) and never returned or logged.
Every route here is under /api, so the security middleware's guards apply (same-origin for POST, the token, the
body limit), and every path ending in /credentials is left out of telemetry (observability.telemetry).
"""

import time
from dataclasses import asdict
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import TypeAdapter

from pengumon.api.common import StateDep, device_or_404, record_answer, record_capped
from pengumon.state import AppState
from pengumon.capabilities import Capability
from pengumon.capabilities.features import feature_status, host_services_status
from pengumon.lines import docsis
from pengumon.lines.docsis import CHANNEL_HISTORY_FIELDS
from pengumon.devices.device import Device
from pengumon.devices.samples import BUCKET_FIELDS, MAX_POINTS_LIMIT, RouterHistory, bucket_width, bucketed_router_history
from pengumon.api.payloads import CredentialsRequest
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.observability import get_logger
from pengumon.secrets import SECRETS_FILE_DAMAGED, SecretsFileUnusable, SecretStore, credential_updates
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")
_log = get_logger("pengumon.api")

_ROUTER_SNAPSHOTS = TypeAdapter(List[RouterMetricsSnapshot])
_HISTORY_CAPABILITIES = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM})
# The longest range the unbucketed history answers: the dashboard asks for at most 24 h (the chart's overlay) and an
# incident's window; 48 h leaves room for a long incident. A week is 55 000 snapshots, half a second in the repository.
UNBUCKETED_HISTORY_MAX_SECONDS = 48 * 3600
_CREDENTIALS_RATE = rate_limit("credentials", 5, 60)  # one bucket for every credentials route


async def _latest(device: Device):
    """The latest snapshot. Never polls: a read by a person must not cost the device one (see Device.current)."""
    return await device.current()


async def _set_credentials(state: AppState, device: Device, payload: CredentialsRequest) -> Dict[str, Any]:
    """Persists the credentials, hands them to the device and polls it once to test them. When a source that ranks
    above secrets.json (the environment, a secret file) supplies this device's credentials, the saved ones are used
    until the next start only: the answer says so in `overridden_by`."""
    device_id = device.id
    store = state.secret_store or SecretStore()
    try:
        store.write(credential_updates(device_id, payload.username, payload.password))
    except SecretsFileUnusable:  # logged by the store, with what to do
        raise HTTPException(status_code=500, detail=SECRETS_FILE_DAMAGED) from None
    device.set_credentials(payload.username, payload.password, source=f"{store.path} (set in the dashboard)")
    snapshot = await device.poll(force=True, bypass_gate=True)
    success = snapshot.status not in ("error", "unconfigured")
    _log.info(f"Credentials for {device_id} saved; test poll {'succeeded' if success else 'failed'}",
              device=device_id, status=snapshot.status, error=getattr(snapshot, "error_kind", None))
    result: Dict[str, Any] = {"success": success, "snapshot": snapshot}
    if snapshot.error_kind:  # the snapshot's own field is not serialized: the dashboard tells a refused sign-in from a silent device
        result["error_kind"] = snapshot.error_kind
    overriding = (state.credentials.outranking(device.config)
                  if state.credentials is not None and device.config is not None else None)
    if overriding:
        _log.warning(f"Credentials for {device_id} were saved to {store.path}, but {overriding} ranks above it: "
                     "they are in use until the next start, then that source's are", device=device_id,
                     overridden_by=overriding)
        result["overridden_by"] = overriding
    return result


def device_ranges(device: Device) -> Dict[str, Dict[str, Any]]:
    """The gauge scale and good zone of each metric the device's snapshot grades (pengumon.grades.Range.as_dict), keyed
    like the snapshot's `grades`. Empty for a device whose readings are not graded."""
    if Capability.ACCESS_LINE in device.driver.offers and getattr(device.driver, "line_technology", None) == docsis.TECHNOLOGY:
        return {name: r.as_dict() for name, r in docsis.ranges(getattr(device.monitor, "levels", None)).items()}
    ranges = getattr(device.monitor, "ranges", None)
    return {name: r.as_dict() for name, r in ranges.items()} if ranges else {}


def device_label(device: Device) -> str:
    """The device as a person names it: what it is (the driver's `label`, e.g. "ASUS router") and its model from
    config.yaml, or its id when no model is set ("Xfinity gateway · modem"); never only an address."""
    kind = getattr(device.driver, "label", None) or device.driver.name
    model = getattr(device.config, "model", None)
    return f"{kind} · {str(model).replace('_', ' ') if model else device.id}"


def device_summary(device: Device) -> Dict[str, Any]:
    """A device as /api/devices lists it: what it is and how its last poll went. Never a credential."""
    snapshot = device.latest_snapshot
    offers = device.driver.offers
    entry: Dict[str, Any] = {
        "id": device.id,
        "driver": device.driver.name,
        "host": device.driver.host,
        "url": getattr(device.config, "url", None),  # validated to carry no credentials
        "model": getattr(device.config, "model", None),
        "label": device_label(device),
        "enabled": device.enabled,
        "offers": sorted(c.value for c in offers),
        "line_technology": getattr(device.driver, "line_technology", None) if Capability.ACCESS_LINE in offers else None,
        "poll_interval_seconds": device.interval_seconds,
        "has_credentials": device.has_credentials,
        "status": snapshot.status if snapshot else None,  # None: not polled yet
        "last_poll": snapshot.timestamp if snapshot else None,
        "ranges": device_ranges(device),
    }
    if snapshot is not None:
        if snapshot.error_kind:
            entry["error_kind"] = snapshot.error_kind
        if snapshot.error_message:
            entry["error_message"] = snapshot.error_message
    return entry


# --- generic --------------------------------------------------------------------------------------------------------

@router.get("/devices")
async def list_devices(state: StateDep):
    """Every configured device, in config order, with the state of its last poll."""
    if state.devices is None:
        raise HTTPException(status_code=503, detail="Devices not initialized")
    return [device_summary(d) for d in state.devices.all()]


@router.get("/devices/{device_id}")
async def get_device(device_id: str, state: StateDep):
    """The device's latest snapshot (never polls the device)."""
    return await _latest(device_or_404(state, device_id))


@router.post("/devices/{device_id}/credentials", dependencies=[Depends(_CREDENTIALS_RATE)])
async def set_device_credentials(device_id: str, payload: CredentialsRequest, state: StateDep):
    """Stores the device's login in data/secrets.json and tests it with one poll."""
    return await _set_credentials(state, device_or_404(state, device_id), payload)


@router.get("/features")
async def get_features(state: StateDep):
    """Which features this installation can offer, and for each one that it cannot, why."""
    if state.devices is None:
        raise HTTPException(status_code=503, detail="Devices not initialized")
    return [asdict(s) for s in (*feature_status(state.devices), host_services_status(state.config))]


@router.get("/devices/{device_id}/history")
async def get_device_history(
    device_id: str,
    state: StateDep,
    start_time: Optional[float] = Query(None),
    end_time: Optional[float] = Query(None),
    bucket_seconds: Optional[int] = Query(None, ge=1),
    max_points: Optional[int] = Query(None, ge=2, le=MAX_POINTS_LIMIT),
):
    """The device's stored polls for incident zoom inspection (RouterMetricsSnapshot rows, oldest first, padded by
    two minutes each side). 404 for an unknown id, 400 for a device that reports neither WAN counters nor router
    system figures. Without bucket_seconds and max_points a range longer than UNBUCKETED_HISTORY_MAX_SECONDS (48 h)
    is cut to its newest 48 h: every poll is one row (a week is 55 000 of them).

    With bucket_seconds and/or max_points the answer is instead bucketed: {"bucket_seconds", "start_time",
    "end_time", "fields", "rows", "capacity"}, rows in field order (avg and max of WAN rx/tx Mbps, CPU %, memory %
    and clients per bucket, no padding). The width is at least bucket_seconds and large enough for max_points
    (default 400) buckets; the range is clipped to the retention. `capacity` is the line capacity in Mbps
    ({"down_mbps", "up_mbps"}, null where unknown), for a reference line."""
    device = device_or_404(state, device_id)
    if not device.driver.offers & _HISTORY_CAPABILITIES:
        raise HTTPException(status_code=400, detail="This device has no router history")
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    now = time.time()
    st = start_time if start_time is not None else (now - 3600)
    et = end_time if end_time is not None else now
    if bucket_seconds is None and max_points is None:
        if et - st > UNBUCKETED_HISTORY_MAX_SECONDS:
            record_capped("device_history", "range")
        st = max(st, et - UNBUCKETED_HISTORY_MAX_SECONDS)  # the newest part of a longer range
        snapshots = await RouterHistory(state.repository, device.id).get_router_snapshots_in_range(st, et)
        body = _ROUTER_SNAPSHOTS.dump_json(snapshots)
        record_answer("device_history", len(snapshots), len(body))
        return Response(body, media_type="application/json")
    st = max(st, now - state.repository.retention_days * 86400)
    width = bucket_width(et - st, bucket_seconds, max_points)
    rows = await bucketed_router_history(state.repository, device.id, st, et, width) if et >= st else []
    record_answer("device_history_bucketed", len(rows))
    load = getattr(state.classifier, "load", None)
    return {"bucket_seconds": width, "start_time": st, "end_time": et, "fields": list(BUCKET_FIELDS), "rows": rows,
            "capacity": {"down_mbps": getattr(load, "down_mbps", None), "up_mbps": getattr(load, "up_mbps", None)}}


@router.get("/devices/{device_id}/channels/history")
async def get_device_channels_history(
    device_id: str,
    state: StateDep,
    start_time: Optional[float] = Query(None),
    end_time: Optional[float] = Query(None),
    channel_id: Optional[int] = Query(None),
    direction: Optional[Literal["downstream", "upstream"]] = Query(None),
):
    """Per-channel history of a DOCSIS line: {"fields", "rows"} with rows [ts, direction ("d"/"u"), channel_id,
    power_dbmv, snr_db, uncorrectables_recent], oldest first; defaults to the last 24 hours, clipped to the
    retention; channel_id and direction narrow it. 404 for an unknown id, 400 for a device without an access line
    or whose line is not DOCSIS."""
    device = device_or_404(state, device_id)
    if Capability.ACCESS_LINE not in device.driver.offers:
        raise HTTPException(status_code=400, detail="This device reports no access line")
    if getattr(device.driver, "line_technology", None) != docsis.TECHNOLOGY:
        raise HTTPException(status_code=400, detail="This device's access line is not DOCSIS")
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    now = time.time()
    st = max(start_time if start_time is not None else now - 86400, now - state.repository.retention_days * 86400)
    et = end_time if end_time is not None else now
    rows = (await docsis.DocsisHistory(state.repository, device.id).get_channel_history(st, et, channel_id, direction)
            if et >= st else [])
    return {"fields": list(CHANNEL_HISTORY_FIELDS), "rows": rows}
