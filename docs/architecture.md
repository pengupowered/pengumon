# Architecture: components

The running app is an ordered list of components, `COMPONENTS` in `src/pengumon/main.py`. `main.py` itself holds
only the app factory (`create_app`: middleware, static files, routers), the lifespan frame (config, logging,
telemetry) and that list. Each feature wires itself in its own module. Adding a feature is a module with a
`register` hook and one line in the list; `tests/test_components.py` checks that nothing else names it.

## The contract

A component is a module with

```python
async def register(ctx: Context) -> Component | None
```

and, optionally, `OPTIONAL = True`, for a feature the app can run without (see below).

`register` builds the feature's parts from `ctx` and returns what the app runs for it, or `None` when the config
gives the feature nothing to do (no enabled service check, no device with a client list, no ntfy URL). `ctx`
(`components.Context`) carries:

- `config`, the loaded `AppConfig`;
- `state`, the `AppState` being built. Request handlers read it through `pengumon.state.get_state`, the gauges through
  `observe_state`, once every component has registered.
- what one component builds for later ones and no handler needs: `notifier`, `line_load`;
- `spawn(coro)`, which starts a background task that runs until shutdown.

The returned `Component` has a `name` and three optional parts:

- `start()`: after every component has registered, in list order. What it is for: starts background work with `ctx.spawn`
- `stop()`: at shutdown, after every spawned task has ended, in reverse list order. What it is for: closes what the feature owns, resolves what nothing will watch any more
- `live_part`: each time the probe loop publishes a message. What it is for: a `LivePart(name, value(diagnosis))`: one field of the live message

Gauges stay in the one table, `observability/gauges.py`. Each row reads the `AppState` (a service gauge reads
`state.service_checker`), so a feature that is not configured simply has no samples. A feature module may append
its own rows. The services gauges stay in the table so that the order of `/metrics` does not change.

## Where things live

- **Routes.** Every HTTP and websocket route is in `api/<feature>.py`, and `api/router.py` lists every router in
  `ROUTERS`. That holds for the features that are components too (`api/devices.py`, `api/services.py`,
  `api/clients.py`, `api/discovery.py`, `api/live.py`): a component has no routes. `create_app` mounts them all at
  import time, whether or not the feature is configured, so the routes and the OpenAPI schema never depend on the
  settings; a route of an unconfigured feature answers as it always has (`[]`, 400 or 503). `/metrics` and the page
  are mounted by `main.py` itself.
- **The `api` package is the top layer.** Route modules import the feature packages; nothing outside `api/` but
  `main.py` imports `api`. What the routes and the lower layers share lives below it: the app state in `state.py`
  (`AppState`, `get_state`), the live stream's broadcaster in `live/ws.py`, the preview job's answer in
  `settings/preview.py`. `api/__init__.py` holds only its docstring, so importing one route module loads only what that
  module needs.
- **Cards (the page).** A card is a folder, `web/static/panels/<id>/` (`web/ui.py` finds it). A card drawn by views
  splits into its model (`model.js`, or `charts.js` for latency: fetching, state, actions, the view model with every
  value formatted) and its standard view (`view.js`: the DOM of its body). A built-in theme may add its own view of a
  card, `web/static/themes/<theme>/panels/<id>.js` (+ `.css`), found by `web/themes.py`, or restyle the standard view
  with a stylesheet alone (`<id>.css`, through the hooks each standard part documents); `web/static/js/cards.js`
  mounts the right view, swaps it on a theme change and falls back to the standard one. The contract and the
  conversion guide are in [themes.md, "Drawing a card"](themes.md#drawing-a-card) and
  ["Restyling a card"](themes.md#restyling-a-card).

## The order, and why

- **1. storage** (`db/storage.py`). Opens (migrates) the database. The settings live in it, every later component may use it, and their `stop` runs before its own, which closes it.
- **2. settings** (`settings/component.py`). The settings store: the one-time import of config.yaml on the first start, then the stored settings (the environment overrides on top) copied into the live config, so every later component is built from them. Creates the one `SecretStore`, puts the hub on `ctx.settings` (components subscribe their appliers for live changes there) and sets the repository's retention days before the start-up cleanup. Puts the sign-in in force (`access/manager.py`: the password from secrets.json or `PENGUMON_ACCESS_TOKEN`, and the viewers setting, into the app's one `SecurityConfig`) before the app answers its first request.
- **3. devices** (`devices/component.py`). Built from the stored devices. A device that cannot run (a missing CA file, say) is logged and left out; the start goes on. Warns about stored history of devices that are not in the settings.
- **4. probing** (`engine/probing.py`). Loads the probe path from the database (`engine/path.py`: the one-time import of config.yaml's targets and the custom targets on a first start), then builds the prober. Line load and the classifier need the complete target list; a path edit later replaces `config.targets` in place, so they keep seeing the live one.
- **5. retention** (`db/retention.py`). The start-up cleanup, before the load backfills read the stored history.
- **6. line_load** (`engine/line_load.py`). Line capacity and the load backfills. Also the cross-wiring: a monitor with `watch_load` gets the line load, one with `watch_probes` gets the probe-loss check.
- **7. notify** (`notify/component.py`). The notifier: ntfy and webhooks (`notify/notifier.py`, `notify/webhook.py`), for the classifier and the service checks.
- **8. classifier** (`engine/classifier/component.py`). Needs the line load, the notifier and the devices' line technology.
- **9. settings_preview** (`settings/preview_component.py`). The job store for the outage-rules preview; its `stop` kills a replay still running.
- **10. services** (`services/component.py`). Optional: host service checks.
- **11. clients** (`clients/component.py`). Optional: the client list.
- **12. live** (`live/component.py`). The broadcaster task that feeds the `/ws` route (`api/live.py`).
- **13. discovery** (`engine/discovery/component.py`). Optional: network discovery on request. Its `stop` (a run still going is cancelled) runs early, because it comes late in the list.
- **14. runtime** (`observability/component.py`). The event-loop monitor (lag and task count for the telemetry). Last in the list; it has nothing to stop.

The repository object exists before any component. The lifespan builds it from `config.database`
(`storage.repository_for`, the bootstrap `db_path`); building it opens nothing.

The order is explicit, never discovered: start-up has real dependencies, and depguard and a reader should see
everything that runs, in the order it runs.

## Start-up failures

- A **required** component's failure in `register` or `start` stops the start, with its own message. That covers
  the devices, the database, the prober and the classifier: the app is no use without them.
- An **optional** component (`OPTIONAL = True`: services, clients, discovery) is contained by the frame. The error is logged
  ("services.component could not register: ...; running without it") and the app starts without that feature.
  One whose `start` failed is still stopped, so whatever it opened gets closed.
- Inside a required component, a step the app can do without is contained by that component. line_load catches a
  failure to learn the capacity or to backfill: it costs the load awareness, never the start.

## Shutdown

`podman stop` sends SIGTERM and kills the container 10 s later. Uvicorn then runs the lifespan's shutdown:

1. Every task the components spawned is cancelled, and the frame waits for all of them. No component's stop runs
   while a probe round, a device poll or a service check is still running.
2. Each component's `stop` runs in reverse list order. A stop that fails is logged and the others still run.
   - discovery cancels a run still going (it writes nothing).
   - settings_preview kills a preview replay still running.
   - The runtime, live, clients, line_load, retention and probing components have nothing to stop. Their tasks have ended.
   - services resolves its open incidents and closes its HTTP clients.
   - classifier ends its path traces (they write to the repository) and resolves the incident still open.
   - notify closes its HTTP client.
   - storage closes the repository. This comes after everything above that writes to it.
   - devices closes the drivers' HTTP clients last. They never write to the repository.
3. The frame removes the AppState and stops the gauges.
4. The frame logs "Pengumon shut down successfully.".
5. The telemetry flush comes last, so the shutdown's own log lines and spans go out too.

`tests/test_components.py::test_shutdown_order` and
`tests/test_lifespan.py::test_sigterm_shuts_the_server_down_cleanly_within_podman_stops_grace_period` (the real entry
point, SIGTERM, 10 s) check this.

## The live message

The probe loop (`engine/loop.py`) builds the message from the round itself and the registered parts:

- the round: `timestamp`, `diagnosis` and `targets`;
- one field per registered `LivePart`:
  - `devices` (summaries, from the devices component);
  - `line` (load and capacity, from line_load);
  - `incidents_version` (from storage).

The message is built only while a dashboard is connected, and `ws.publish` only stores it for the broadcaster task.
A part must name a field of `LiveStreamMessage` that the round does not fill, and only one part may fill each field.
The probing component checks this when it starts the loop. A new live field is therefore a field in
`live/message.py`, the wire contract, and the feature's `LivePart`.
