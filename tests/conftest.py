"""Shared fixtures."""

import pytest

from pengumon.db.repository import DatabaseRepository


@pytest.fixture(autouse=True)
async def _close_repositories(monkeypatch):
    """The repository keeps one connection open for its lifetime; close it at the end of each test."""
    opened = []
    original = DatabaseRepository.initialize

    async def tracked(self, *args, **kwargs):
        opened.append(self)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(DatabaseRepository, "initialize", tracked)
    yield
    for repo in opened:
        await repo.close()


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Rate-limit buckets are process-wide; tests that make many calls must not trip each other."""
    from pengumon import security
    security.reset_rate_limits()
    yield
    security.reset_rate_limits()


@pytest.fixture
def app_state(monkeypatch):
    """A fresh, empty AppState on the app for one test; set the parts a test needs on it (handlers read it through
    the api.state.get_state dependency). Removed again afterwards."""
    from pengumon.api.state import STATE_ATTR, AppState
    from pengumon.main import app
    state = AppState()
    monkeypatch.setattr(app.state, STATE_ATTR, state, raising=False)
    return state
