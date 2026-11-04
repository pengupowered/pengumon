"""The component contract: how a feature plugs into the app. The design is in docs/architecture.md.

A component is a module with `async def register(ctx) -> Component | None`, listed once in pengumon.main.COMPONENTS.
`register` builds the feature's parts from `ctx` (the config, the AppState being built, what earlier components left
on the context) and returns what the app runs for it, or None when the config gives the feature nothing to do. A
module may also have a module-level `router`, which the app factory mounts whether or not the feature is configured
(the routes and the OpenAPI schema never depend on config.yaml), and `OPTIONAL = True`, which contains a failure of
its register or start: the error is logged and the app runs without that feature.

`run_components` is the lifespan's middle: register every component in list order, install the AppState, start them
in list order; at shutdown cancel every task they spawned, then stop them in reverse order.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Coroutine, List, Optional, Sequence, Tuple

from pengumon.api.state import STATE_ATTR, AppState
from pengumon.observability import get_logger
from pengumon.observability.export import observe_state
from pengumon.observability.telemetry import task_exits

if TYPE_CHECKING:
    from fastapi import FastAPI

    from pengumon.config import AppConfig
    from pengumon.engine.load import LineLoad
    from pengumon.incidents.model import SystemDiagnosis
    from pengumon.notify import Notifier
    from pengumon.settings.hub import SettingsHub

_log = get_logger("pengumon.components")


@dataclass(frozen=True)
class LivePart:
    """One field of the live message (a LiveStreamMessage field name) and how to read it. `value` is called with
    the round's diagnosis each time the probe loop publishes a message, so it must be quick and never wait."""

    name: str
    value: Callable[["SystemDiagnosis"], Any]


def task_kind(task: "asyncio.Task") -> str:
    """What a task runs, as its coroutine's qualified name ("Device.run"): fixed in the code, so it is a metric label
    (the task's own name, "Task-17", is not)."""
    coro = task.get_coro()
    return getattr(coro, "__qualname__", None) or type(coro).__name__


def log_unexpected_exit(task: "asyncio.Task") -> None:
    """Done-callback for a task that should run until shutdown: logs the exception it died of, when it dies (not at
    shutdown, when its result is collected). A cancelled task is not an unexpected exit."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        kind = task_kind(task)
        task_exits.add(1, {"kind": kind, "error": type(exc).__name__})
        _log.error(f"Background task {task.get_name()} stopped: {type(exc).__name__}: {exc}", kind=kind, exc_info=exc)


@dataclass
class Component:
    """What the app runs for one feature. Every part is optional.

    `start` runs after every component has registered, in list order, and starts the feature's background work with
    ctx.spawn. `stop` runs at shutdown, after every spawned task has ended, in reverse list order; it closes what the
    feature owns. `live_part` is the feature's field of the live message."""

    name: str
    start: Optional[Callable[[], Awaitable[None]]] = None
    stop: Optional[Callable[[], Awaitable[None]]] = None
    live_part: Optional[LivePart] = None


@dataclass
class Context:
    """What register(ctx) gets. `state` is the AppState being built; it reaches the request handlers once every
    component has registered. The other fields are what one component builds for the later ones and no request
    handler needs."""

    app: "FastAPI"
    config: "AppConfig"
    state: AppState
    settings: Optional["SettingsHub"] = None  # settings: subscribe appliers for live setting changes (settings/hub.py)
    notifier: Optional["Notifier"] = None  # notify, for the classifier and the service checks
    line_load: Optional["LineLoad"] = None  # line_load, for the classifier
    components: List[Component] = field(default_factory=list)  # registered so far, in list order
    tasks: List[asyncio.Task] = field(default_factory=list)

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Starts a background task that runs until shutdown, when it is cancelled before any component stops."""
        task = asyncio.create_task(coro)
        task.add_done_callback(log_unexpected_exit)
        task.add_done_callback(self._forget)  # a finished task (a rebuilt device's old one) is not kept
        self.tasks.append(task)
        return task

    def _forget(self, task: "asyncio.Task") -> None:
        try:
            self.tasks.remove(task)
        except ValueError:
            pass

    def live_parts(self) -> Tuple[LivePart, ...]:
        """The live-message parts of the registered components, in list order."""
        return tuple(c.live_part for c in self.components if c.live_part is not None)


def _name(module) -> str:
    return module.__name__.removeprefix("pengumon.")


async def _contained(module, what: str, step: Callable[[], Awaitable[Any]]) -> Tuple[bool, Any]:
    """Runs a register or start step. A required component's failure stops the start; an optional one's is logged
    and costs only that feature. Returns (ok, result)."""
    if not getattr(module, "OPTIONAL", False):
        return True, await step()
    try:
        return True, await step()
    except Exception as e:
        _log.error(f"{_name(module)} could not {what}: {e}; running without it", component=_name(module),
                   exc_info=True)
        return False, None


@asynccontextmanager
async def run_components(app: "FastAPI", config: "AppConfig", modules: Sequence,
                         state: AppState) -> AsyncIterator[Context]:
    """Registers and starts `modules` (each with register(ctx)) and installs `state` (the AppState to build, holding
    the config and the repository) on the app; stops them again when the block ends. A required component's failure
    before the yield propagates: the start fails."""
    ctx = Context(app=app, config=config, state=state)
    registered = []  # (module, component), for the start step
    for module in modules:
        ok, component = await _contained(module, "register", lambda m=module: m.register(ctx))
        if ok and component is not None:
            ctx.components.append(component)
            registered.append((module, component))

    # The composition root: request handlers get it through api.state.get_state, the gauges through observe_state.
    setattr(app.state, STATE_ATTR, state)
    observe_state(state)

    for module, component in registered:
        if component.start is not None:
            await _contained(module, "start", component.start)  # an optional one that failed is still stopped
    try:
        yield ctx
    finally:
        tasks = list(ctx.tasks)  # (a task that ends leaves the list)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for component in reversed(ctx.components):
            if component.stop is None:
                continue
            try:
                await component.stop()
            except Exception as e:
                _log.error(f"{component.name} did not stop cleanly: {e}", component=component.name, exc_info=True)
        observe_state(None)
        if getattr(app.state, STATE_ATTR, None) is not None:
            delattr(app.state, STATE_ATTR)
