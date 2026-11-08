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


@pytest.fixture
def repo_options():
    """Extra DatabaseRepository arguments for `repo`; a module overrides this fixture (test_storage.py does)."""
    return {}


@pytest.fixture
async def repo(tmp_path, repo_options):
    """An initialized DatabaseRepository on tmp_path/repo.db. A module with its own `repo` fixture uses that one."""
    repository = DatabaseRepository(db_path=str(tmp_path / "repo.db"), **repo_options)
    await repository.initialize()
    yield repository
    await repository.close()


@pytest.fixture(autouse=True)
def _no_real_config_home(monkeypatch):
    """The service code puts a user's unit under $XDG_CONFIG_HOME when it is set (GitHub's runners and many desktops set
    it): a test that points the home directory at tmp_path would otherwise read, and write, the real
    ~/.config/systemd/user/pengumon.service of whoever runs the tests. Tests that want it set it themselves."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


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
    the state.get_state dependency). Removed again afterwards."""
    from pengumon.state import STATE_ATTR, AppState
    from pengumon.main import app
    state = AppState()
    monkeypatch.setattr(app.state, STATE_ATTR, state, raising=False)
    return state


@pytest.fixture
async def mcp_state(tmp_path, monkeypatch):
    """The seeded AppState of mcp_helpers (MCP on, the sign-in token not set) installed on the app for one test."""
    from mcp_helpers import build_state
    from pengumon.main import app, security_config
    from pengumon.mcp.tools import reset_pack_rate
    from pengumon.state import STATE_ATTR

    reset_pack_rate()  # the MCP pack limit is per process: each test starts with none built
    state = await build_state(tmp_path)
    monkeypatch.setattr(app.state, STATE_ATTR, state, raising=False)
    monkeypatch.setattr(security_config, "access_token", "")
    yield state
    await state.repository.close()


MAX_TEST_ID = 16000  # half of what Windows allows: it caps an environment variable at 32,767 characters, and pytest puts the id in one


def pytest_collection_modifyitems(config, items):
    """A parametrized test whose id holds a long value (a 70 KiB body) failed on Windows only, in setup: pytest sets
    PYTEST_CURRENT_TEST to the id. Refuse such ids on every platform, so the Linux run says so first."""
    long = [item.nodeid[:120] + "..." for item in items if len(item.nodeid) > MAX_TEST_ID]
    if long:
        raise pytest.UsageError(f"test ids longer than {MAX_TEST_ID} characters (give the parametrize short ids): "
                                + "; ".join(long))
