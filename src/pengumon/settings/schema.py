"""What `GET /api/settings/schema` sends: the registry as the page needs it, with each setting's type, range,
choices and default read from its pydantic field, the four lists' item fields, the device drivers and their options,
and the secret slots. The same for every request of a build (only `round_seconds` comes from the config)."""

from typing import Any, Dict, List, Type

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from pengumon.drivers import DRIVERS
from pengumon.settings import registry


def item_field(name: str, info: FieldInfo) -> Dict[str, Any]:
    """One field of a list item or of a driver's options: {name, required, default, type, ...constraints}; a nested
    model (a device's `tls`) has its own `fields`."""
    spec: Dict[str, Any] = {"name": name, "required": info.is_required(),
                            "default": None if info.is_required() else _json(registry.field_default(info)),
                            **registry.field_constraints(info)}
    inner = registry.unwrap_optional(info.annotation)[0]
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        spec["fields"] = model_fields(inner)
    return spec


def model_fields(model: Type[BaseModel]) -> List[Dict[str, Any]]:
    return [item_field(name, info) for name, info in model.model_fields.items()]


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def setting_spec(setting: registry.Setting) -> Dict[str, Any]:
    return {"key": setting.key, "section": setting.section, "group": setting.group, "label": setting.label,
            "effect": setting.effect, "unit": setting.unit, "scale": setting.scale, "rounds": setting.rounds,
            "live": setting.live, "env": setting.env, "advanced": setting.advanced, "export": setting.export,
            "order": setting.order, "related": setting.related, "also_in": list(setting.also_in),
            **setting.constraints(), "default": _json(setting.default())}


def driver_spec(driver) -> Dict[str, Any]:
    return {"label": driver.label, "default_poll_seconds": driver.default_poll_seconds,
            "min_poll_seconds": getattr(driver, "min_poll_seconds", 0.0),
            "default_username": getattr(driver, "default_username", None) or "admin",
            "url_schemes": list(getattr(driver, "url_schemes", ("http", "https"))),
            "options": model_fields(driver.options_model)}


def list_spec(spec: registry.ListSpec) -> Dict[str, Any]:
    out = {"label": spec.label, "section": spec.section, "path": spec.path,
           "item_fields": model_fields(spec.item_model), "default_items": _json(spec.default())}
    if spec.name == "devices":
        out["drivers"] = {name: driver_spec(driver) for name, driver in sorted(DRIVERS.items())}
    return out


def build_schema(round_seconds: float) -> Dict[str, Any]:
    """The schema document (api/settings_models.SettingsSchema)."""
    return {
        "round_seconds": round_seconds,
        "sections": [{"id": s.id, "title": s.title, "order": s.order} for s in registry.SECTIONS],
        "settings": [setting_spec(s) for s in registry.SETTINGS],
        "lists": {name: list_spec(spec) for name, spec in registry.LISTS.items()},
        "secrets": [{"slot": s.slot, "label": s.label, "kind": s.kind, "section": s.section, "env": s.env,
                     "pattern": s.pattern} for s in registry.SECRET_SLOTS],
    }
