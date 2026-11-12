"""The Settings API: the settings store (pengumon.settings.manager)
read, validated, saved, versioned, exported and imported. Bodies: api/settings_models.py.

GET  /api/settings/schema         the registry with types, ranges, defaults, the lists' fields, drivers, secret slots
GET  /api/settings                the current state (a viewer gets the lists without what only the owner needs, and no
                                  secret hosts): every value with its source, the lists, the secret slots (set
                                  or not, never a value), the learned capacity and, for an editor, "about this install"
GET  /api/settings/values         {revision, values}: the effective values only
POST /api/settings/validate       a change {values, lists} -> {ok, errors, warnings, summary}; nothing is applied
PUT  /api/settings                {base_revision, values, lists, secrets, current_password?} -> the new revision
                                  (?source=restore for a version loaded with GET /versions/{n}, ?source=yaml_import for
                                  an Import draft); turning access.allow_viewers on needs current_password while a
                                  password is in force (api/access.py says why)
GET  /api/settings/versions       the kept versions, newest first (a viewer's without the header secrets' names)
GET  /api/settings/versions/{n}   that version as a draft (saving it restores it)
GET  /api/settings/export         the stored values and lists as YAML, no secrets (needs the token: VIEWER_POLICY)
POST /api/settings/import         YAML in the body -> a draft {values, lists, notes}; nothing is applied

Every non-GET needs the token. Errors: {"detail", "errors": [{"where", "message"}]}; a 409 adds the current revision
and the conflicting parts.
"""

from typing import Dict, Optional
from urllib.parse import urlsplit

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from pengumon import __version__, paths
from pengumon.api.access import NEED_CURRENT_VIEWERS, unconfirmed
from pengumon.api.common import StateDep
from pengumon.api.settings_models import (
    About, ConfigYamlState, ConflictError, ImportDraft, ListState, PutResponse, SecretState, SettingsValidate,
    SettingsError, SettingsPut, SettingsSchema, SettingsState, SettingsValues, ValidateResponse, ValueState,
    VersionDraft, VersionInfo,
)
from pengumon.state import AppState
from pengumon.db.settings import SettingsConflict
from pengumon.observability import grafana
from pengumon.security import can_edit, rate_limit
from pengumon.settings.env import env_value
from pengumon.settings.manager import SecretsWriteFailed, SettingsInvalid, SettingsManager, _host
from pengumon.settings.schema import build_schema

router = APIRouter(prefix="/api")

_SAVE_RATE = rate_limit("settings", 20, 60)
VIEWERS_KEY = "access.allow_viewers"


class SettingsPutBody(SettingsPut):
    """PUT /api/settings: SettingsPut, plus the current password for a save that lets others look without it."""
    current_password: Optional[str] = Field(None, max_length=1024)


def _manager(state: AppState) -> SettingsManager:
    if state.settings is None:
        raise HTTPException(status_code=503, detail="Settings not initialized")
    return state.settings


def url_origin(url: object) -> str:
    """scheme://host[:port] of a URL: no user name, path or query (an API key often rides in one of them)."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname
        if not host or not parts.scheme:
            return ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{'[' + host + ']' if ':' in host else host}{port}"
    except ValueError:
        return ""


def _for_viewer(name: str, item: dict) -> dict:
    """A list item as someone without the password sees it in the read-only Settings page: what the dashboard shows
    anyway (ids, names, on or off, the device's address), without what only the owner needs (a device's user name
    and driver options, which hold the mesh labels; a check's path, query and CA file path; a webhook's headers)."""
    item = dict(item)
    if name == "devices":
        item["url"] = url_origin(item.get("url"))
        item["username"] = ""
        item["options"] = {}
        if isinstance(item.get("tls"), dict):
            item["tls"] = {**item["tls"], "ca_file": None}
    elif name == "services":
        item["url"] = url_origin(item.get("url"))
        item["ca_file"] = None
    elif name == "webhooks":
        item["headers"] = {}
    return item


def _turns_viewers_on(manager: SettingsManager, values: dict) -> bool:
    """Whether a save of `values` stores `access.allow_viewers` as on while it is not stored so (null resets it to its
    default, off). Compared with what is stored, not with what is in force: a value stored under
    PENGUMON_VIEW_WITHOUT_TOKEN=0 takes effect once the variable goes."""
    if VIEWERS_KEY not in values or values[VIEWERS_KEY] is None or values[VIEWERS_KEY] is False:
        return False
    return manager.value_states().get(VIEWERS_KEY, {}).get("stored") is not True


def _learned(state: AppState) -> Dict[str, Optional[float]]:
    load = getattr(state.classifier, "load", None)
    return {"down_mbps": getattr(load, "learned_down_mbps", None), "up_mbps": getattr(load, "learned_up_mbps", None)}


def _about(request: Request, manager: SettingsManager, state: AppState) -> About:
    host, port, _, _ = paths.listen_address(manager.config.server)
    security = getattr(request.app.state, "security_config", None)
    return About(
        listen=f"{host}:{port}",
        data_dir=str(paths.data_dir()),
        config_path=str(paths.config_path()),
        db_path=getattr(state.repository, "db_path", None),
        token_set=bool(getattr(security, "token_set", False)),
        view_without_token=bool(getattr(security, "view_without_token", False)),
        otlp_host=_host(env_value("OTEL_EXPORTER_OTLP_ENDPOINT")),
        config_yaml=ConfigYamlState(**manager.config_yaml_state()),
        import_report=manager.import_report(),
        version=__version__,
    )


def _error(status: int, detail: str, errors=()) -> JSONResponse:
    return JSONResponse(status_code=status, content=SettingsError(detail=detail, errors=list(errors)).model_dump())


def _etag() -> str:
    return f'"settings-schema-{__version__}"'


@router.get("/settings/schema", response_model=SettingsSchema)
async def get_settings_schema(request: Request, response: Response, state: StateDep):
    """The registry as the page needs it. Static per build: ETag of the version, revalidated on every use."""
    if state.config is None:
        raise HTTPException(status_code=503, detail="Settings not initialized")
    headers = {"ETag": _etag(), "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == _etag():
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return SettingsSchema.model_validate(build_schema(state.config.prober.interval_seconds))


@router.get("/settings", response_model=SettingsState)
async def get_settings(request: Request, state: StateDep):
    """Every setting's effective value and source, the lists, the secret slots (set or not, never a value), the
    learned line capacity, and for an editor the install's paths and the import report."""
    manager = _manager(state)
    editor = can_edit(request)
    lists = manager.list_states()
    errors = getattr(state.devices, "errors", None)
    if isinstance(errors, dict):
        lists["devices"]["errors"] = {str(k): str(v) for k, v in errors.items()}
    secrets = manager.secret_states()
    if not editor:  # a viewer sees whether a secret is set, not which host it points at, and no header secret (its name names the header)
        lists = {k: {**v, "items": [_for_viewer(k, i) for i in v["items"]]} for k, v in lists.items()}
        secrets = {k: {**v, "host": None} for k, v in secrets.items() if not k.startswith("webhook_header.")}
    return SettingsState(
        revision=manager.revision,
        values={k: ValueState(**v) for k, v in manager.value_states().items()},
        lists={k: ListState(**v) for k, v in lists.items()},
        secrets={k: SecretState(**v) for k, v in secrets.items()},
        learned=_learned(state), about=_about(request, manager, state) if editor else None, can_edit=editor)


@router.get("/settings/grafana-links")
async def get_grafana_links() -> list:
    """The header's Grafana links as they are now (the page refills its menu after a Save of display.grafana_url)."""
    return grafana.links()


@router.get("/settings/values", response_model=SettingsValues)
async def get_settings_values(state: StateDep):
    """{revision, values: {key: effective value}}."""
    manager = _manager(state)
    return SettingsValues(revision=manager.revision, values=manager.values())


@router.post("/settings/validate", response_model=ValidateResponse,
             dependencies=[Depends(rate_limit("settings_validate", 60, 60))])
async def validate_settings(body: SettingsValidate, state: StateDep):
    """Checks a change against the current state; nothing is applied. `warnings` are not refusals."""
    checked = await _manager(state).validate(body.values, body.lists, body.secrets)
    return ValidateResponse(ok=checked.ok, errors=checked.errors, warnings=checked.warnings, summary=checked.summary)


@router.put("/settings", response_model=PutResponse, dependencies=[Depends(_SAVE_RATE)],
            responses={400: {"model": SettingsError}, 409: {"model": ConflictError}, 422: {"model": SettingsError},
                       500: {"model": SettingsError}})
async def put_settings(body: SettingsPutBody, request: Request, state: StateDep,
                       source: str = Query("save", pattern="^(save|restore|yaml_import)$")):
    """Saves a change as the next revision and applies it. values[key] = null resets a key, lists[name] = null a
    list, secrets[slot] = "" clears a slot. A save (a restore, an import) that turns viewing without the password on
    needs the current password while one is in force: a browser left signed in must not open the dashboard to everyone
    (403 without it or with a wrong one, which counts as a failed sign-in)."""
    manager = _manager(state)
    if _turns_viewers_on(manager, body.values):
        why = await unconfirmed(request, body.current_password, NEED_CURRENT_VIEWERS)
        if why is not None:
            return _error(403, why, [{"where": f"values.{VIEWERS_KEY}", "message": why}])
    try:
        applied = await manager.apply(body.base_revision, body.values, body.lists, body.secrets, source=source)
    except SettingsInvalid as e:
        return _error(e.status, e.detail, e.errors)
    except SettingsConflict as e:
        return JSONResponse(status_code=409, content=ConflictError(
            detail="Changed elsewhere since you loaded the settings", revision=e.current,
            conflicts=e.conflicts).model_dump())
    except SecretsWriteFailed as e:
        return _error(500, e.detail)
    return PutResponse(revision=applied.revision, summary=applied.summary, applied=applied.applied,
                       restart_needed=applied.restart_needed, live_failed=applied.live_failed)


# "secret webhook_header.<name> changed" in a version line (settings/describe.py): the name names a header secret
_HEADER_SLOT = re.compile(r"\bsecret webhook_header\..*? changed(?=; |$)", re.IGNORECASE)


def _summary_for_viewer(summary: str) -> str:
    """A version line as someone without the password sees it: a header secret's change without its name, which
    /api/settings keeps from them too (it names the header)."""
    return _HEADER_SLOT.sub(lambda m: m.group(0)[0] + "ecret of a webhook header changed", summary)  # "s" or "S"


@router.get("/settings/versions", response_model=list[VersionInfo])
async def get_settings_versions(request: Request, state: StateDep):
    """The kept versions, newest first. A viewer (or the read token) gets the lines without the header secrets'
    names."""
    versions = await _manager(state).versions()
    if not can_edit(request):
        versions = [{**v, "summary": _summary_for_viewer(v["summary"])} for v in versions]
    return [VersionInfo(**v) for v in versions]


@router.get("/settings/versions/{revision}", response_model=VersionDraft)
async def get_settings_version(revision: int, request: Request, state: StateDep):
    if not can_edit(request):  # a kept version is a whole document, as the export is
        raise HTTPException(status_code=401, detail="Sign in to read a kept version")
    draft = await _manager(state).version_draft(revision)
    if draft is None:
        raise HTTPException(status_code=404, detail="That version is no longer kept")
    return VersionDraft(**draft)


@router.get("/settings/export")
async def export_settings(state: StateDep):
    """The stored values and set lists as YAML in config.yaml syntax, without secrets."""
    return Response(_manager(state).export_yaml(), media_type="application/yaml",
                    headers={"Content-Disposition": 'attachment; filename="pengumon-settings.yaml"'})


@router.post("/settings/import", response_model=ImportDraft,
             dependencies=[Depends(rate_limit("settings_import", 20, 60))])
async def import_settings(request: Request, state: StateDep):
    """Reads YAML (an export, or a whole config.yaml) into a draft for the page. Nothing is applied."""
    manager = _manager(state)
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        draft = await manager.import_yaml(text)
    except ValueError as e:
        return _error(422, str(e))
    return ImportDraft(**draft)
