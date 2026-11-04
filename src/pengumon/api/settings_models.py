"""The request and response bodies of the Settings API (api/settings.py, api/settings_preview.py): the one place
their shapes are written down. docs/api.md describes them; the frontend and the store code against these.

Conventions:
- A key is a registry key ("classifier.down_loss_pct"); a list name is one of devices, services, webhooks,
  dns_probes; a slot is a secret slot ("ntfy.url", "webhook.<id>.url", "webhook_header.<name>").
- Values are stored units (rounds, fractions, seconds); the page converts with the schema's scale and rounds.
- A secret's value is only ever sent *to* the server (PUT `secrets`), never back.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["default", "stored", "env"]
SecretSource = Literal["env", "secrets_file", "secret_file"]
VersionSource = Literal["import", "save", "restore", "yaml_import"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- errors (every non-2xx answer of a settings endpoint) ----

class FieldError(_Model):
    """`where`: "values.<key>", "lists.<name>[<index>].<field>" or "secrets.<slot>". `message` never repeats the
    submitted value."""
    where: str
    message: str


class SettingsError(_Model):
    detail: str
    errors: List[FieldError] = Field(default_factory=list)


class ConflictError(_Model):
    """409 from PUT: parts this request touched changed since `base_revision`."""
    detail: str
    revision: int
    conflicts: List[str]  # keys and list names


# ---- GET /api/settings/schema ----

class SectionSpec(_Model):
    id: str
    title: str
    order: int


FieldType = Literal["number", "integer", "boolean", "text", "choice", "time", "timezone", "layout", "list",
                    "object", "map"]


class Constraints(_Model):
    type: FieldType
    nullable: bool
    min: Optional[float] = None
    max: Optional[float] = None
    exclusive_min: bool = False
    exclusive_max: bool = False
    max_length: Optional[int] = None
    step: Optional[float] = None  # 1 for integers; None: any
    choices: Optional[List[Any]] = None


class SettingSpec(Constraints):
    key: str
    section: str
    group: Optional[str]
    label: str
    effect: str
    unit: Optional[str]
    scale: float
    rounds: bool  # stored in probe rounds; shown as value x round_seconds seconds
    live: bool
    env: Optional[str]
    advanced: bool
    export: bool
    order: int
    related: Optional[str]
    also_in: List[str]
    default: Any


class ItemFieldSpec(Constraints):
    name: str
    required: bool
    default: Any
    fields: Optional[List["ItemFieldSpec"]] = None  # a nested model's fields (a device's tls)


class DriverSpec(_Model):
    label: str
    default_poll_seconds: float
    min_poll_seconds: float
    default_username: str
    url_schemes: List[str]
    options: List[ItemFieldSpec]  # mesh_node_labels is type "map" (MAC -> label)


class ListSchema(_Model):
    label: str
    section: str
    path: str  # where it lives in config.yaml / an export ("service_checks.services")
    item_fields: List[ItemFieldSpec]
    default_items: List[Dict[str, Any]]
    drivers: Optional[Dict[str, DriverSpec]] = None  # devices only


class SecretSpec(_Model):
    slot: str  # "ntfy.url"; a pattern: "webhook.{id}.url", "webhook_header.{name}"
    label: str
    kind: Literal["url", "token"]
    section: str
    env: Optional[str]
    pattern: bool


class SettingsSchema(_Model):
    round_seconds: float
    sections: List[SectionSpec]
    settings: List[SettingSpec]
    lists: Dict[str, ListSchema]
    secrets: List[SecretSpec]


# ---- GET /api/settings ----

class ValueState(_Model):
    value: Any  # the effective value (env > stored > default)
    stored: Any = None  # the stored value; None when the key is not stored
    source: Source
    pending_restart: bool = False
    live_failed: bool = False


class ListState(_Model):
    set: bool  # False: the list's default (`items` are then the defaults)
    items: List[Dict[str, Any]]  # webhooks without url; header values are "secret:<name>"
    errors: Dict[str, str] = Field(default_factory=dict)  # item id -> problem (a device that could not be built)


class SecretState(_Model):
    set: bool
    source: Optional[SecretSource] = None
    host: Optional[str] = None  # URL slots only


class ConfigYamlState(_Model):
    ignored_since: Optional[float] = None
    differing_keys: int = 0


class About(_Model):
    """Sent only when can_edit."""
    listen: str
    data_dir: str
    config_path: str
    db_path: Optional[str]
    token_set: bool
    view_without_token: bool
    otlp_host: Optional[str]
    config_yaml: Optional[ConfigYamlState] = None
    import_report: List[Dict[str, Any]] = Field(default_factory=list)  # [{key, reason}], never a value
    version: str


class SettingsState(_Model):
    revision: int  # 0: no settings store yet (the W0 stub)
    values: Dict[str, ValueState]
    lists: Dict[str, ListState]
    secrets: Dict[str, SecretState]
    learned: Dict[str, Optional[float]]  # {"down_mbps", "up_mbps"}: the learned line capacity, None while unknown
    about: Optional[About] = None
    can_edit: bool


class SettingsValues(_Model):
    """GET /api/settings/values: the effective values only, for help texts and the page template."""
    revision: int
    values: Dict[str, Any]


# ---- POST /api/settings/validate, PUT /api/settings ----

class SettingsChange(_Model):
    """A change against the current state: values[key] = None resets the key, lists[name] = None resets the list."""
    values: Dict[str, Any] = Field(default_factory=dict)
    lists: Dict[str, Optional[List[Dict[str, Any]]]] = Field(default_factory=dict)


class ValidateResponse(_Model):
    ok: bool
    errors: List[FieldError] = Field(default_factory=list)
    warnings: List[FieldError] = Field(default_factory=list)  # not refusals ("retention under 7 days: ...")
    summary: str = ""  # the version line the save would write


class SettingsValidate(SettingsChange):
    secrets: Dict[str, str] = Field(default_factory=dict)  # slot -> value typed but not saved; "" clears the slot


class SettingsPut(SettingsValidate):
    base_revision: int


class PutResponse(_Model):
    revision: int
    summary: str
    applied: List[str]  # keys and list names
    restart_needed: List[str] = Field(default_factory=list)
    live_failed: List[str] = Field(default_factory=list)  # committed, applies at the next start


# ---- versions, export, import ----

class VersionInfo(_Model):
    revision: int
    applied_at: float
    source: VersionSource
    summary: str


class VersionDraft(_Model):
    """GET /api/settings/versions/{n}: the document of that version, as a draft (saving it restores it)."""
    revision: int
    values: Dict[str, Any]
    lists: Dict[str, Optional[List[Dict[str, Any]]]]


class ImportDraft(_Model):
    """POST /api/settings/import: a draft from YAML; nothing is applied."""
    values: Dict[str, Any]
    lists: Dict[str, Optional[List[Dict[str, Any]]]]
    notes: List[str] = Field(default_factory=list)
    kept: List[str] = Field(default_factory=list)  # settings whose value in the file was refused: keep the current one


# ---- the outage-rules preview (api/settings_preview.py) ----

class PreviewRequest(_Model):
    values: Dict[str, Any] = Field(default_factory=dict)  # classifier.*, line.down_mbps / up_mbps, alerts.* only
    days: Optional[int] = Field(None, ge=1)  # default min(7, retention_days)


class PreviewCounts(_Model):
    incidents: int
    critical: int
    warning: int
    busy_hidden: int
    total_seconds: float
    alerts: Optional[int] = None  # incident alerts the notifier's rules would have sent (engine/replay.py)


class PreviewWindow(_Model):
    start: float
    end: float
    rounds: int
    gaps: List[Dict[str, float]] = Field(default_factory=list)  # [{start, end}]


class PreviewChange(_Model):
    start: float
    end: Optional[float]
    kind: str
    severity: str
    in_: Literal["draft", "current", "changed"] = Field(alias="in")
    was_start: Optional[float] = None  # "changed": the current rules' start, end and severity of the same outage
    was_end: Optional[float] = None
    was_severity: Optional[str] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PreviewJob(_Model):
    """POST /api/settings/preview answers {"id": ...}; GET /api/settings/preview/{id} answers this."""
    id: Optional[str] = None
    state: Literal["running", "done", "failed", "cancelled"]
    progress: float = 0.0
    elapsed_s: float = 0.0
    error: Optional[str] = None  # "took too long"
    window: Optional[PreviewWindow] = None
    current: Optional[PreviewCounts] = None
    draft: Optional[PreviewCounts] = None
    recorded: Optional[int] = None
    changes: List[PreviewChange] = Field(default_factory=list)  # up to 30
    notes: List[str] = Field(default_factory=list)
