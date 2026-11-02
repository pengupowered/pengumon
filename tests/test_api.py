"""Unit tests for FastAPI endpoints."""

import pytest
from httpx import AsyncClient, ASGITransport
from pengumon.main import app


@pytest.mark.asyncio
async def test_dashboard_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/")
        assert response.status_code == 200
        assert "<title>pengumon</title>" in response.text and "Path health" in response.text
        assert "Hop details" in response.text
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"



@pytest.mark.asyncio
async def test_api_info_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/info")
        assert response.status_code == 200
        data = response.json()
        assert "lan_ips" in data
        assert "primary_lan_url" in data
        assert len(data["lan_ips"]) > 0


@pytest.mark.asyncio
async def test_input_validation_rejections():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Invalid target with command injection chars
        bad_target = {
            "id": "bad;id",
            "name": "Bad Target",
            "address": "1.1.1.1; rm -rf /",
            "category": "custom",
            "type": "icmp"
        }
        res = await ac.post("/api/targets", json=bad_target)
        assert res.status_code == 422  # Unprocessable Entity / Validation Error

        # Invalid traceroute host with injection characters
        bad_trace = {"target_host": "1.1.1.1 && whoami"}
        res2 = await ac.post("/api/mtr/trace", json=bad_trace)
        assert res2.status_code == 422


@pytest.mark.asyncio
async def test_incident_inspect_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/incidents/99999/inspect")
        # Nonexistent incident returns 404 or empty
        assert res.status_code in (404, 503)
