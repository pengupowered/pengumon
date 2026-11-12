"""The page carries the settings revision its values were rendered at (<body data-settings-revision>), so
js/settings-values.js can tell a Save made after the render from the first live message."""
import json
import re
from html import unescape
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.config import AppConfig
from pengumon.main import app
from pengumon.state import STATE_ATTR, AppState


async def _page(settings) -> str:
    setattr(app.state, STATE_ATTR, AppState(config=AppConfig(), settings=settings))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return (await ac.get("/")).text
    finally:
        delattr(app.state, STATE_ATTR)


def _body_tag(html: str) -> str:
    return re.search(r"<body\b[^>]*>", html).group(0)


@pytest.mark.asyncio
async def test_the_page_carries_the_revision_its_settings_values_were_rendered_at():
    # the values and the revision come from the same object in one step; a made-up store stands in for the real one
    store = SimpleNamespace(revision=17, values=lambda: {"incidents.group_gap_minutes": 7})
    tag = _body_tag(await _page(store))
    assert re.search(r'\bdata-settings-revision="17"', tag)
    assert json.loads(unescape(re.search(r"\bdata-settings='([^']*)'", tag).group(1))) == {"incidents.group_gap_minutes": 7}


@pytest.mark.asyncio
async def test_revision_zero_is_carried_too_and_a_page_without_a_store_has_none():
    tag = _body_tag(await _page(SimpleNamespace(revision=0, values=lambda: {"a.b": 1})))
    assert re.search(r'\bdata-settings-revision="0"', tag)
    tag = _body_tag(await _page(None))
    assert "data-settings-revision" not in tag
    assert re.search(r"\bdata-settings='\{\}'", tag)
