"""v16: the stored names that were spelled the British way take the US spelling (the incident kind and titles of a
hop that deprioritizes ping, the one-time meta markers, the dashboard's card colors setting)."""

import json

import aiosqlite

VERSION = 16

_INCIDENT_KINDS = {"hop_deprioritised": "hop_deprioritized"}
_INCIDENT_TITLES = {"ISP Hop Deprioritises Ping": "ISP Hop Deprioritizes Ping",
                    "Line Busy: ISP Hops Deprioritise Ping": "Line Busy: ISP Hops Deprioritize Ping"}
_META_KEYS = {"settings_initialised": "settings_initialized", "path_initialised": "path_initialized",
              "incident_ping_deprioritisation_backfill": "incident_ping_deprioritization_backfill"}
_SETTING_KEYS = {"dashboard.colours": "dashboard.colors"}


async def _rename_keys(db: aiosqlite.Connection, table: str, names: dict) -> None:
    for old, new in names.items():  # a row already under the new name wins (the old one is dropped)
        await db.execute(f"UPDATE OR IGNORE {table} SET key = ? WHERE key = ?", (new, old))
        await db.execute(f"DELETE FROM {table} WHERE key = ?", (old,))


async def migrate(db: aiosqlite.Connection):
    """Renames in place; idempotent."""
    for old, new in _INCIDENT_KINDS.items():
        await db.execute("UPDATE incidents SET kind = ? WHERE kind = ?", (new, old))
    for old, new in _INCIDENT_TITLES.items():
        await db.execute("UPDATE incidents SET title = ? WHERE title = ?", (new, old))
    await _rename_keys(db, "meta", _META_KEYS)
    await _rename_keys(db, "settings_values", _SETTING_KEYS)
    # the saved versions of the settings too, so going back to one restores its card colors
    async with db.execute("SELECT revision, document FROM settings_versions") as cursor:
        rows = await cursor.fetchall()
    for revision, raw in rows:
        document = json.loads(raw)
        values = document.get("values") if isinstance(document, dict) else None
        if not isinstance(values, dict) or not any(old in values for old in _SETTING_KEYS):
            continue
        for old, new in _SETTING_KEYS.items():
            if old in values:
                values.setdefault(new, values[old])
                del values[old]
        await db.execute("UPDATE settings_versions SET document = ? WHERE revision = ?",
                         (json.dumps(document, separators=(",", ":"), ensure_ascii=False), revision))
