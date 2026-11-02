"""The dashboard must not depend on third-party origins; the vendored Chart.js must be the verified file."""

import hashlib
import re
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from pengumon.main import app

EXPECTED_SHA256 = "08dfa4730571b23810c34fc39c5101461ecafca56c3f92caf4850509cb158f30"
VENDOR_FILE = Path(__file__).resolve().parents[1] / "src" / "pengumon" / "web" / "static" / "vendor" / "chart.umd.min.js"


def test_vendored_chart_js_hash_on_disk():
    assert hashlib.sha256(VENDOR_FILE.read_bytes()).hexdigest() == EXPECTED_SHA256


@pytest.mark.asyncio
async def test_vendored_chart_js_served_with_expected_hash():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/static/vendor/chart.umd.min.js")
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == EXPECTED_SHA256


@pytest.mark.asyncio
async def test_dashboard_has_no_third_party_script_and_csp_has_no_cdn():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/")
    assert response.status_code == 200
    assert not re.search(r"<script[^>]+src=[\"']https?:", response.text, re.IGNORECASE)
    assert re.search(r'src="/static/v/[0-9a-f]{12}/vendor/chart\.umd\.min\.js"', response.text)
    csp = response.headers["Content-Security-Policy"]
    assert "jsdelivr" not in csp
    assert "http" not in csp
