"""The component list (docs/architecture.md): adding a feature edits only main.py's COMPONENTS, start-up failures of an
optional feature cost only that feature, and shutdown keeps its order (every task ends first, the path traces and the
service incidents before the repository closes, the telemetry flush last)."""

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from pengumon import main
from pengumon.components import Component

SRC = Path(__file__).resolve().parent.parent / "src" / "pengumon"

# What main.py may import besides the component modules: the app frame (FastAPI, config loading, middleware, the
# static files and page, the core API, logging and telemetry). A feature wired into main.py directly fails here.
FRAME_MODULES = {
    "pengumon", "pengumon.__version__", "pengumon.api.router", "pengumon.state", "pengumon.components", "pengumon.config",
    "pengumon.observability", "pengumon.observability.export", "pengumon.observability.telemetry",
    "pengumon.paths", "pengumon.security", "pengumon.web",
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)


def _defines_register(tree: ast.Module) -> bool:
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "register" for n in tree.body)


def _runtime_imports(tree: ast.Module):
    """(line, module) of every import outside an `if TYPE_CHECKING:` block; `from a import b` yields a and a.b."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            skip |= {id(n) for n in ast.walk(node)}
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


SOURCES = {_module_name(p): ast.parse(p.read_text(encoding="utf-8")) for p in sorted(SRC.rglob("*.py"))}
LISTED = [m.__name__ for m in main.COMPONENTS]


# --- guards: adding a component edits only main.py's list ------------------------------------------------------------

def test_every_module_with_a_register_hook_is_in_the_component_list_and_only_once():
    with_register = sorted(name for name, tree in SOURCES.items() if _defines_register(tree))
    assert sorted(LISTED) == with_register, "a module with register(ctx) is a component: list it in main.COMPONENTS"
    assert len(set(LISTED)) == len(LISTED)


def test_no_module_but_main_imports_a_component_module():
    """So the list in main.py is the one place that names a component."""
    offenders = [f"{name}:{line} imports {imported}" for name, tree in SOURCES.items() if name != "pengumon.main"
                 for line, imported in _runtime_imports(tree) if imported in LISTED]
    assert offenders == []


def test_main_imports_only_the_components_and_the_app_frame():
    imported = {m for _, m in _runtime_imports(SOURCES["pengumon.main"]) if m.split(".")[0] == "pengumon"}
    # `from pengumon.db import retention` yields pengumon.db too; a package counts when one of its modules is listed
    packages = {name.rsplit(".", 1)[0] for name in LISTED}
    stray = sorted(m for m in imported if m not in LISTED and m not in packages and m not in FRAME_MODULES
                   and m.rsplit(".", 1)[0] not in FRAME_MODULES - {"pengumon"})
    assert stray == [], f"main.py wires a feature directly: {stray}"


def test_the_list_starts_with_the_storage_the_settings_and_the_devices():
    """The storage first (the settings live in it, and every later component's stop may still write to it), then
    the settings (which load the stored settings into the config every later component is built from), then the
    devices, built from the stored devices."""
    names = [m.__name__.removeprefix("pengumon.") for m in main.COMPONENTS]
    assert names[:3] == ["db.storage", "settings.component", "devices.component"]
    assert names.index("engine.line_load") < names.index("engine.classifier.component")
    assert names.index("notify.component") < min(names.index("engine.classifier.component"), names.index("services.component"))
    assert names.index("db.retention") < names.index("engine.line_load")  # clean up before the backfills read


# --- the lifespan runs the list -------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _root_level_kept():
    """The lifespan applies server.log_level to the root logger; later tests expect it as they found it."""
    level = logging.getLogger().level
    yield
    logging.getLogger().setLevel(level)


def _config_file(tmp_path, extra: str = "") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database:\n  db_path: " + repr(str(tmp_path / "pengumon.db")) + "\n"
        "targets:\n  - {id: gw, name: Gateway, address: 192.168.100.1, category: gateway}\n" + extra,
        encoding="utf-8")
    return str(path)


async def _idle(*args, **kwargs):
    await asyncio.sleep(3600)


def _quiet_workers(monkeypatch):
    from pengumon.db import retention
    from pengumon.engine import probing
    from pengumon.services import component as services
    monkeypatch.setattr(probing, "probe_background_worker", _idle)
    monkeypatch.setattr(retention, "cleanup_background_worker", _idle)
    monkeypatch.setattr(services, "collector_background_worker", _idle)


class _FakeModule:
    """A component module made up for the test: register(ctx) records its calls."""

    def __init__(self, name, events, fail=None, optional=False):
        self.__name__ = f"pengumon.fake.{name}"
        self.name, self.events, self.fail = name, events, fail
        if optional:
            self.OPTIONAL = True

    async def register(self, ctx):
        self.events.append((self.name, "register", ctx.state.classifier is not None))
        if self.fail == "register":
            raise RuntimeError("boom")

        async def start():
            self.events.append((self.name, "start"))
            if self.fail == "start":
                raise RuntimeError("boom")
            ctx.spawn(_idle())

        async def stop():
            self.events.append((self.name, "stop", all(t.done() for t in ctx.tasks)))

        return Component(self.name, start=start, stop=stop)


async def test_a_component_added_to_the_list_is_registered_started_and_stopped(tmp_path, monkeypatch):
    """Adding a feature is one more entry in COMPONENTS: it registers after the classifier, starts, and stops first,
    after every task has ended."""
    from pengumon.state import STATE_ATTR
    events = []
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(tmp_path))
    _quiet_workers(monkeypatch)
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, _FakeModule("extra", events)])
    async with main.lifespan(main.app):
        assert getattr(main.app.state, STATE_ATTR).classifier is not None
        assert events == [("extra", "register", True), ("extra", "start")]
    assert events[-1] == ("extra", "stop", True)


@pytest.mark.parametrize("step", ["register", "start"])
async def test_an_optional_components_failure_costs_only_that_feature(tmp_path, monkeypatch, caplog, step):
    from httpx import ASGITransport, AsyncClient
    events = []
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(tmp_path))
    _quiet_workers(monkeypatch)
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, _FakeModule("flaky", events, fail=step, optional=True),
                                             _FakeModule("after", events)])
    with caplog.at_level(logging.ERROR, logger="pengumon"):
        async with main.lifespan(main.app):
            async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
                assert (await ac.get("/api/targets")).status_code == 200
    assert ("after", "start") in events and ("after", "stop", True) in events
    assert [r.getMessage() for r in caplog.records if "flaky" in r.getMessage()] == [
        f"fake.flaky could not {step}: boom; running without it"]


async def test_a_required_components_failure_stops_the_start(tmp_path, monkeypatch):
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(tmp_path))
    _quiet_workers(monkeypatch)
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, _FakeModule("core", [], fail="register")])
    with pytest.raises(RuntimeError, match="boom"):
        async with main.lifespan(main.app):
            pass


async def test_a_stop_that_fails_is_logged_and_the_rest_still_stop(tmp_path, monkeypatch, caplog):
    from pengumon.db.repository import DatabaseRepository
    closed = []
    real_close = DatabaseRepository.close

    async def close(self):
        closed.append(True)
        await real_close(self)

    class Breaks(_FakeModule):
        async def register(self, ctx):
            async def stop():
                raise RuntimeError("stuck")
            return Component("breaks", stop=stop)

    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(tmp_path))
    _quiet_workers(monkeypatch)
    monkeypatch.setattr(DatabaseRepository, "close", close)
    monkeypatch.setattr(main, "COMPONENTS", [*main.COMPONENTS, Breaks("breaks", [])])
    with caplog.at_level(logging.ERROR, logger="pengumon"):
        async with main.lifespan(main.app):
            pass
    assert closed == [True]
    assert "breaks did not stop cleanly: stuck" in caplog.text


async def test_shutdown_order(tmp_path, monkeypatch):
    """Every spawned task ends before any component stops; the path traces end and the open service incidents are
    resolved before the repository closes; the devices' and the notifier's HTTP clients close; the telemetry flush
    comes last, after the final log line."""
    from pengumon.components import Context
    from pengumon.db.repository import DatabaseRepository
    from pengumon.devices.registry import DeviceRegistry
    from pengumon.engine.classifier import OutageClassifier
    from pengumon.notify.notifier import Notifier
    from pengumon.services.checker import ServiceChecker

    events = []
    tasks = []
    real_spawn = Context.spawn

    def spawn(self, coro):
        tasks.append(real_spawn(self, coro))
        return tasks[-1]

    monkeypatch.setattr(Context, "spawn", spawn)

    def spy(cls, method, label):
        real = getattr(cls, method)

        async def wrapper(self, *args, **kwargs):
            events.append((label, all(t.done() for t in tasks)))
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(cls, method, wrapper)

    spy(DatabaseRepository, "close", "repository closed")
    spy(OutageClassifier, "cancel_background", "path traces ended")
    spy(ServiceChecker, "resolve_open", "service incidents resolved")
    spy(ServiceChecker, "aclose", "service clients closed")
    spy(DeviceRegistry, "aclose", "device clients closed")
    spy(Notifier, "aclose", "notifier closed")

    class Telemetry:
        def shutdown(self):
            events.append(("telemetry flushed", True))

    real_info = main.logger.info

    def info(msg, *args, **kwargs):
        if msg == "Pengumon shut down successfully.":
            events.append(("last log line", True))
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(main, "setup_telemetry", Telemetry)
    monkeypatch.setattr(main.logger, "info", info)
    monkeypatch.setenv("PENGUMON_NOTIFY_URL", "http://127.0.0.1:9/topic")
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, "service_checks:\n  services:\n    - {id: app, name: App, url: 'http://127.0.0.1:9/health'}\n"
                  'devices:\n  - {id: wifi, driver: asuswrt, url: "http://192.0.2.1", enabled: false}\n'))
    _quiet_workers(monkeypatch)
    async with main.lifespan(main.app):
        # the wifi device, probe loop, retention, line load, speed test scheduler, service checks, broadcaster,
        # route-change checks, the event-loop monitor
        assert len(tasks) == 9

    labels = [label for label, _ in events]
    assert all(done for _, done in events), events  # every task had ended
    assert set(labels) == {"repository closed", "path traces ended", "service incidents resolved",
                           "service clients closed", "device clients closed", "notifier closed", "last log line",
                           "telemetry flushed"}
    assert labels.index("path traces ended") < labels.index("repository closed")
    assert labels.index("service incidents resolved") < labels.index("repository closed")
    assert labels[-2:] == ["last log line", "telemetry flushed"]


async def test_a_spawned_task_that_dies_is_logged_when_it_dies_and_a_canceled_one_is_not(caplog):
    from pengumon.components import Context

    async def crash():
        raise RuntimeError("boom")

    ctx = Context(app=None, config=None, state=None)
    idle = ctx.spawn(asyncio.sleep(3600))
    with caplog.at_level(logging.ERROR, logger="pengumon.components"):
        task = ctx.spawn(crash())
        await asyncio.wait([task])
        await asyncio.sleep(0)
        assert [r for r in caplog.records if "boom" in r.getMessage()]
        caplog.clear()
        idle.cancel()
        await asyncio.gather(idle, return_exceptions=True)
        await asyncio.sleep(0)
    assert not caplog.records
