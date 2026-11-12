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
  `main.py` imports `api` (tests/test_module_layout.py). What the routes and the lower layers share lives below it: the
  app state in `state.py` (`AppState`, `get_state`), the live stream's broadcaster in `live/ws.py`, the preview job's
  answer in `settings/preview.py`, and what the MCP tools share with the routes in the feature packages
  (`devices/summary.py`, `services/summary.py`, the router's traffic figures and events in `devices/traffic.py` and `devices/events.py`, the AI pack lock in `reports/common.py`). `api/__init__.py` holds only its docstring, so importing one route module loads only what that
  module needs.
- **Outages.** The joining rule is one implementation, `incidents/outages.py` (`OutageBuilder`: pure, no I/O, told the
  time), with its settings in `incidents/settings.py` (`incidents:`). `incidents/outage_read.py` reads the incidents an
  outage needs from the repository (widening the window until the grouping is exact). Nothing is stored: outages are
  computed on every read, by `api/outages.py` and `api/incidents.py` (`outage_id`), `reports/outages.py` (the report, the
  dossier and the AI pack), the MCP `incidents` tool, and live by the notifier (`notify/notifier.py`, one outage at a
  time, for the alerts) and the settings preview (`engine/replay.py`). See [diagnosis.md](diagnosis.md#outages).
- **Cards (the page).** A card is a folder, `web/static/panels/<id>/` (`web/ui.py` finds it). A card drawn by views
  splits into its model (`model.js`, or `charts.js` for latency: fetching, state, actions, the view model with every
  value formatted) and its standard view (`view.js`: the DOM of its body). A built-in theme may add its own view of a
  card, `web/static/themes/<theme>/panels/<id>.js` (+ `.css`), found by `web/themes.py`, or restyle the standard view
  with a stylesheet alone (`<id>.css`, through the hooks each standard part documents); `web/static/js/cards.js`
  mounts the right view, swaps it on a theme change and falls back to the standard one. The helpers the cards share are
  small modules named for what they hold (`js/targets.js`, `dom.js`, `clock.js`, `format.js`, `escape.js`, `modal.js`, ...:
  [dashboard.md](dashboard.md)); the two large dialogs are folders of modules too (`dialogs/settings/`: sections, fields,
  render, validation, save, transfer, versions; `dialogs/target-history/`: state, build, charts, figures, strips, load).
  Every dialog is opened through `js/modal.js`, which owns the browser history entry that Back closes
  ([dashboard.md, "Back and dialogs"](dashboard.md)). The contract and the
  conversion guide are in [themes.md, "Drawing a card"](themes.md#drawing-a-card) and
  ["Restyling a card"](themes.md#restyling-a-card).

- **Storage reads.** `db/core.py` opens the database with one writer connection and `READERS` (3) read-only ones (the
  vendored homeguard `Database(readers=...)`), so a slow report never holds up the page's other reads. The rollup reads
  (`db/rollups.py`: whole quarters from `rollup_15m`, the edges from `rollup_1m`) and the kept answers of the report,
  loss grid and uptime strips (`reports/cache.py`, at most two builds at once, fewer than the readers) are described in
  [storage.md](storage.md#rollups-and-retention-schema-v18).
- **Router traffic.** The ASUS driver's Traffic Analyzer reads are `drivers/asuswrt_traffic.py`; the SNMPv3 session is
  `drivers/snmp_v3.py` (with `drivers/snmp_ber.py`); the stored figures are read and explained by `devices/traffic.py`,
  the restart and WAN link events are `devices/events.py`, and both are served by `api/traffic.py`
  ([devices.md](devices.md#asuswrt-traffic-uptime-wan-link-and-temperature)). The dashboard side is the `traffic` card
  (`web/static/panels/traffic/`) and `web/static/js/live-delta.js` puts the live stream's deltas back together
  ([api.md](api.md): `WS /ws`).

- **Probes.** A hop is probed by ping (`engine/icmp_*.py`), by a TCP handshake (`engine/probe_tcp.py`) or by an HTTP(S)
  request (`engine/probe_http.py`); `engine/probe_rules.py` is the policy for the last two (no loopback, link-local or
  special-purpose destination, at most `MAX_SOCKET_HOPS` of them) and `engine/prober.py` calls the right one for each
  target. The policy is in [security.md](security.md#what-the-paths-tcp-and-http-probes-can-reach).
- **Settings.** `settings/manager.py` keeps the document and the environment; the rest is in modules of one job each:
  `registry.py` (every key, its range and text, the lists and their maximum lengths), `validate.py` (validation, with a
  `ValidationContext` the steps share), `candidate.py` (a document as a config), `transfer.py` (export and import as
  YAML), `describe.py` (the version line of a change), `dotted.py` (dotted paths into mappings), `importer.py` (the one-time import of config.yaml),
  `check.py` (`pengumon settings check-import`) and `preview.py` (the outage-rules preview).
- **Alerts.** `notify/notifier.py` keeps the decisions (which incident or outage alerts, and when); `scheduler.py` has
  the timers, `messages.py` the alert texts, `delivery.py` the ntfy delivery, `webhook.py` the webhook delivery and
  `channels.py` builds the channels from the settings.
- **Drivers.** The pure parsers of the ASUS router's answers are `drivers/asuswrt_parse.py`; every driver that speaks HTTP
  gets its client from `drivers/http.py` (`new_client`) and reads answers through that module's reader, which limits their size and total time.
- **Reports.** The Ask AI pack is built in steps: `reports/ai_findings.py` collects the facts from the stored data,
  `reports/ai_pack_view.py` turns them into the view (the sentences Pengumon writes), and `reports/ai_pack.py` renders
  that view as Markdown at one of three levels through the redactor in `reports/redact.py` (which also holds the final
  scan's rules); `reports/contributions.py` (`build_ai_pack`) ties them together with the device modules. The dossier
  and the reliability report (`reports/dossier.py`, `reliability.py`) share their per-cause totals.
- **Backup.** `GET /api/backup` is `api/backup.py`; the copy is made by `db/snapshot.py` (`VACUUM INTO`) in a private
  temporary folder that `db/backup_temp.py` opens, checks and empties.
- **The page's files.** `web/preload.py` lists the start's modules for `modulepreload`, and `web/assets.py` joins the
  panels' and dialogs' stylesheets into `panels.css` and keeps the gzipped text assets ([dashboard.md](dashboard.md)).

## The order, and why

- **1. storage** (`db/storage.py`). Opens (migrates) the database. The settings live in it, every later component may use it, and their `stop` runs before its own, which closes it.
- **2. settings** (`settings/component.py`). The settings store: the one-time import of config.yaml on the first start, then the stored settings (the environment overrides on top) copied into the live config, so every later component is built from them. Creates the one `SecretStore`, puts the hub on `ctx.settings` (components subscribe their appliers for live changes there) and sets the repository's retention days before the start-up cleanup. Puts the sign-in in force (`access/manager.py`: the password from secrets.json (its hash, and the session secret) or `PENGUMON_ACCESS_TOKEN`, the read token and the viewers setting, into the app's one `SecurityConfig`) before the app answers its first request.
- **3. devices** (`devices/component.py`). Built from the stored devices. A device that cannot run (a missing CA file, say) is logged and left out; the start goes on. Warns about stored history of devices that are not in the settings.
- **4. probing** (`engine/probing.py`). Loads the probe path from the database (`engine/path.py`: the one-time import of config.yaml's targets and the custom targets on a first start), then builds the prober. Line load and the classifier need the complete target list; a path edit later replaces `config.targets` in place, so they keep seeing the live one.
- **5. retention** (`db/retention.py`). The start-up cleanup, before the load backfills read the stored history (it finishes first, as before: the cleanup deletes in batches of `PRUNE_BATCH_ROWS` rows, each its own write transaction, so the probe loop's sample flush gets in between them; it is not run in the background).
- **6. line_load** (`engine/line_load.py`). Line capacity and the load backfills. Also the cross-wiring: a monitor with `watch_load` gets the line load, one with `watch_probes` gets the probe-loss check.
- **7. notify** (`notify/component.py`). The notifier: ntfy and webhooks (`notify/notifier.py`, `notify/webhook.py`), for the classifier and the service checks. It sends one alert per outage, not per incident ([webhooks.md](webhooks.md)), and follows `incidents:` changes live.
- **8. classifier** (`engine/classifier/component.py`). Needs the line load, the notifier and the devices' line technology.
- **9. speedtest** (`speedtest/component.py`). Optional: the scheduled speed test. Needs the line load, the notifier and the classifier (it masks the classifier's verdicts while a test runs). It always registers; the scheduler reads `speedtest:` at every tick, so the setting needs no restart. Its `stop` ends a test still running, which records itself as interrupted.
- **10. settings_preview** (`settings/preview_component.py`). The job store for the outage-rules preview; its `stop` kills a replay still running.
- **11. services** (`services/component.py`). Optional: host service checks.
- **12. clients** (`clients/component.py`). Optional: the client list.
- **13. live** (`live/component.py`). The broadcaster task that feeds the `/ws` route (`api/live.py`).
- **14. discovery** (`engine/discovery/component.py`). Optional: network discovery on request. Its `stop` (a run still going is canceled) runs early, because it comes late in the list.
- **15. runtime** (`observability/component.py`). The event-loop monitor (lag and task count for the telemetry; `observability/runtime.py` also reads the process's memory, threads and handles, on macOS and Windows through `observability/platform_stats.py`). Last in the list; it has nothing to stop.

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

1. Every task the components spawned is canceled, and the frame waits for all of them. No component's stop runs
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

A `/ws?delta=1` connection (the dashboard) gets each target's `recent_latencies` window in full only in its first message and
when it asks again, and otherwise the probes since its last message (`latency_tail`, `live/ws.py` `latency_delta`).

The message is built only while a dashboard is connected, and `ws.publish` only stores it for the broadcaster task.
A part must name a field of `LiveStreamMessage` that the round does not fill, and only one part may fill each field.
The probing component checks this when it starts the loop. A new live field is therefore a field in
`live/message.py`, the wire contract, and the feature's `LivePart`.
