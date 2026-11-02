"""v8: vendor-neutral cause code."""

import aiosqlite

VERSION = 8


async def migrate(db: aiosqlite.Connection):
    """Vendor-neutral cause code: xfinity_last_mile_coax_drop -> access_line_drop (any line technology). The
    models read the old name as the new one too, so a row an older build writes later is still grouped right;
    an older build cannot read the new name, so going back needs the pre-deploy database backup."""
    await db.execute("UPDATE incidents SET cause_code = 'access_line_drop' "
                     "WHERE cause_code = 'xfinity_last_mile_coax_drop'")
