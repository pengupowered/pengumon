# Dashboard

The dashboard is a single page served by the FastAPI app: plain ES modules (no build step), a strict CSP
(`script-src 'self'`, no inline script or handlers) and one live WebSocket. This page describes what a person sees and
how the code is organized. Tests that guard it: `scripts/test.sh` (`tests/test_frontend.py`) and `scripts/test-ui.sh`
(a headless-browser smoke test and ESLint).

## The header

Left to right:

- **Status pill**: the classifier's verdict in a word ("Healthy", "Degraded", "Down", "Busy"), a status dot and, when
  there is something to say, the worst loss on the path and the latency to the internet. The page tint and the tab
  title follow it. Until the first message it is gray and says "Connecting"; the page never shows green before data.
- **Live indicator**: "Live · 1 s" while the socket is open, "Reconnecting" when it is not.
- **LAN address chip** (when the server knows its LAN URL) with a copy button.
- **Tools**: Trace (route trace), Dossier (ISP outage dossier), **Ask AI** (an icon: the troubleshooting pack, below), Alerts (browser notifications on or off), Target (add a
  probe target), **Settings** (every setting of Pengumon), **Layout** (panel arrangement and card colors), the **theme menu** (theme, light or dark, animated effects; docs/themes.md), and a **Grafana** menu with links to the dashboards
  when `PENGUMON_GRAFANA_URL` is set (partial `templates/features/grafana.html`; without it the button is not rendered).
  On a phone the tools collapse into a "more" menu that copies them.

## Panels

One card group per panel, each in its own folder. What a panel needs, and what it does without it:

- Path health (`diagnosis`)
  - Tab: Live
  - Needs: nothing
  - Without it: always shown
- Latency (`latency`)
  - Tab: Live
  - Needs: nothing
  - Without it: always shown
- Hop details (`matrix`)
  - Tab: Live
  - Needs: nothing
  - Without it: always shown
- Uptime (`strips`)
  - Tab: Live
  - Needs: nothing
  - Without it: always shown
- Line (`line`)
  - Tab: Line
  - Needs: a device offering `access_line`
  - Without it: no card, no Line tab, listed in the Layout dialog with the reason; the path chain goes router to ISP hops
- Speed (`speed`)
  - Tab: Network
  - Needs: nothing (the speed test itself is off until it is turned on in Settings > Speed test)
  - Without it: always shown; while the test is off it says so, with a link to the setting
- Router (`router`)
  - Tab: Network
  - Needs: a device offering `wan_counters` or `router_system` (one card per such device); the client and mesh blocks need the `clients` / `mesh` features
  - Without it: no card; a missing client list shows one muted line saying why
- Host services (`services`)
  - Tab: Network
  - Needs: the `host_services` feature (the Host app checks setting)
  - Without it: no card, with the reason in the Layout dialog
- Incidents (`incidents`)
  - Tab: Incidents
  - Needs: nothing
  - Without it: always shown
- When it breaks (`heat`)
  - Tab: Incidents
  - Needs: nothing
  - Without it: always shown
- Reliability (`report`)
  - Tab: Incidents
  - Needs: nothing
  - Without it: always shown

Availability is decided by each panel's `available(ctx)` (or `instances(ctx)`), from `/api/devices` and `/api/features`.
A panel that is not available is never mounted. The page with no devices at all still works (the smoke test's `bare`
variant).

**Speed** is the scheduled speed test ([speedtest.md](speedtest.md)): the last result as two large figures (download and
upload, each with its share of your plan under it when Settings > Your line is set; the line's rate your router measured
during the test, with the test's own figure small under it, or the test's own when no router gave one, as the line under
the figures says: "Line, measured by your router" or "Test only"), when it ran (the full time on hover) and
against which server, the delay under load with its grade (A+ to F), a 7 or 30 day history chart with the plan as dashed
lines, and the tests that failed or were skipped as marks on the axis (a cross and a hollow circle). A result that is not
whole says why: *Partial* with the reason, or *Failed* / *Skipped* above the last good figures. Under the chart: when the next
test is due, or "Speed test is off" with a link that opens Settings at the section, and this month's data use against the
monthly limit. **Run now** asks first ("Uses up to 25 MB down and 10 MB up of data.") and is off, with the reason as its
tooltip, while the test is off, one is running, or the monthly limit is reached; a viewer without the password sees it
marked view only. The card polls once a minute, and every 3 seconds while a test runs. Run now's progress is "Testing
download, then upload...": the server does not report which direction it is in. The card's status is good while the newest
test worked and unknown otherwise: a test that failed raises no warning dot on the Network tab.

**Path health** is the hero. It shows the verdict, then the chain this host, router, line, every ISP hop, internet,
built from the configured targets' categories (DNS probes stand in when there is no public target). Each node has a
short label (Router, Gateway, ISP hop 1, Internet) with the configured target name as a second line (whole in the tooltip),
the median of the last probes, loss, jitter and a sparkline; each link shows the latency the segment adds. A node opens
that target's history. The Internet node (several public servers) and a folded run of ISP hops open the history of all
their targets together, titled as the node is ("Internet · median of 3 public servers", "ISP · 9 hops"): **All** draws a
line per member in its own color and the node's figure as a bold line (the median, or the last hop that answers), loss
per member, a strip per member, and figures by the node's rules; a switcher shows one member as its own history. A
node opens its history on the Live range (its own figure is of the last minute), shows that figure as it is now ("On
the path now: 10.9 ms, the median of the servers' last 60 probes", from the same `path.js` `pathNodes` that paints the
node), and every figure in the dialog says the window it covers ("Median, last 5 min"). With more than five nodes (four in a medium card) the links and nodes get tighter, and in a
medium-width card (about 1024 px) the node icons go and the latency pills move under the gaps. When even that leaves a
node too little room (a long traceroute's worth of ISP hops), the ISP hops fold into one node: the worst hop sets its
status and the last hop that answers its latency; Hop details still lists each one. ISP hops the owner grouped in the
path editor ("one node with the hop before", the targets' `group`) are one node the same way. In a narrow card
(container query, 860 px) the chain turns vertical and shows every hop.

**Edit** in the card's header opens the path editor (`dialogs/path-editor`): the hops as cards, a draft that goes live
only on Apply (configuration.md, "The probe path"). While no path was ever applied (a fresh install probing the default
targets) the card starts discovery by itself and shows a banner, "Pengumon found your network path · Review", that
opens the editor with discovery's draft. After Apply the page reloads, so every panel draws the new path.

A hop that never answers ping while a later hop does is up (the probes pass through it; many provider routers ignore
ping): it reads "Ignores ping" in gray, its loss counts nowhere, and the next link measures from the last hop that
answered. The server decides it, once (`engine/path_rules.py`, from every target's status after each probe round), and
flags the target with `ignores_ping` in the live message and `/api/targets`; the page only reads the flag
(`static/js/path.js` `ignoringPing`, `quietTargets`). Every view follows it: Hop details says "no ping", Uptime and the target history give
it no outage blocks and no availability (its blocks open no incidents), the report's Every hop table lists it as
"Ignores ping", "When it breaks" (`/api/report/loss-grid`) leaves its probes out, the dossier never names it as the
failing hop, and an incident does not list it among its affected targets. The flag never decides whether an incident
opens. It describes the hop as it is now, so the longer views apply the current verdict to their whole window. Custom targets are chips under the chain ("Also watched"); with no path target at all the chain says so.

An IPv6 target while this host has no IPv6 (no route, or none in the operating system) is shown the same neutral way
with its own words: "IPv6 unavailable on this host" on its node ("Not probed", "not probed · no IPv6 here"), "no IPv6"
and "not probed" in Hop details, and that line where its Uptime strip and history strip would be. The prober decides it
(`engine/prober.py`, checked every five minutes) and flags the target with `ipv6_unavailable`; it records no samples
then, counts in no figure and opens nothing, and a redundant group (the public servers) is judged by the members it
does probe. The page reads both flags through `static/js/path.js` `quietTargets` (target id to its words).

The same short labels name a target everywhere else (`static/js/path.js` `shortLabels`): Router, Gateway, ISP hop N,
Internet (with its address when there are several public servers), DNS lookup N, a custom target by its name; the
configured name is the secondary text.

**Latency** draws three path series by default: Router (worst local target), Gateway (worst gateway target) and Internet
(median of the public servers of the IPv4 group; DNS lookups stand in when there is none). When discovery or the path editor put IPv6 servers in their own group (`internet_v6`), they are a fourth line, "Internet (IPv6)", as Path health shows them as their own node. The legend names each series' targets, the
tooltip gives every member's own value, and a note says what the view leaves out. **Each target** draws one thin line
per pinged target (ISP hops included, DNS lookups not: they time a lookup, not a round trip) in the colors Hop details
uses (a target's configured `color`, else the next of eight validated series colors in path order, then the first one no other target of its category has, so the members of one group always differ; gray past eight in one category); the choice is kept per browser. Under the chart a thin loss lane shows the worst target's loss and names it in
the tooltip. The chart takes the height its row gives the card.

**Hop details**: every figure covers the last 60 probes except "Lost since start". Jitter is the median change from one
answered probe to the next, so one slow probe does not lift it. A hop's dot judges its loss over the probes the
outage rules judge (Settings, Outage rules, "Judge each hop by its last"; 10 by default). Click a row for the target's
history. Sparklines are drawn by `static/js/sparkline.js` (SVG, not Chart.js); a lost probe is a red tick and a gap.

**When it breaks** (loss by day and hour) and **Uptime** (a strip per target) are ways into the incident log: every hour
or block that has begun opens the Incidents panel filtered to that time (on a phone, the Incidents tab), where each
incident opens its inspector as usual. A time with nothing to list says why: nothing probed, every probe answered,
busy-line incidents hidden, or loss too small or brief to open an incident.
Their days and hours, and the Reliability report's, are in the zone set in Settings, else in the browser's own zone
(the page sends it), and follow daylight saving: the day the clocks go back has 25 hours (the repeated hour counts twice
in its cell, and opens both) and the day they go forward 23. The server says when each cell begins and ends, so a click
opens the right stretch even when the browser runs in another zone than the one set in Settings; Uptime's times and
dates read on that zone's clock.

The **router** card's client and mesh counts come from the device's live summary (`clients`: counts only, never a
client row); only the Local network dialog fetches the client list.

Each card has a "..." menu: move up, move down, hide, Layout.

## Layout

The **Layout** dialog (`dialogs/layout/`, logic in `static/js/layout.js`) lists the panels with up/down buttons and a
switch each, applied at once. "Reset to default" returns to the default. Panels that are not available on this install
are listed apart with the reason. A panel with several cards (one per device) is one row; its cards move together, so
adding a device needs no layout change.

Two layers:

1. The **default**, from the server: `dashboard:` in the settings (Settings > Export shows it as YAML), `order:` (these panels first, the rest follow in
   their usual order) and `hidden:` (not shown). Unknown or repeated ids are refused when you save (the message lists the valid ids, which are the
   panel folder names); in a `config.yaml` read on the first start they are logged and the default kept. It reaches the page on `<body data-layout>`.
2. The **browser's own choice**, in `localStorage` (`pengumon.layout.v1`) after the first change. It wins over the
   default, so the phone and the PC can differ. A stored layout never hides a panel added later.

Cards fill a 12-column grid (`data-size` 4, 8 or 12); the last card of a row that would leave a gap widens.

## Settings

The **Settings** button (or `#settings` at the end of the address) opens one full-height dialog (`dialogs/settings/`)
with the sections in this order: Outage rules, Alerts, Your line, Speed test, Devices and services, Probing, Retention, Sign-in,
Privacy, Discovery, Display, a collapsed Advanced area ("Change these only if you know why") and "About this install". A rail
on the left jumps between them; at 720 px and below it is a "Jump to section" list and the dialog is full screen.

- **A row** is the label and input (numbers in their own unit: seconds, milliseconds, minutes, percent, "per
  million"; choices in words, such as "Every incident" / "Outages only"), then the effect in one line and, under the
  input, the standard value in one form ("Standard: 15 %", "Standard: none", or what an empty box means), the badges
  and **Reset to standard**. Badges: *Changed* (stored, not the standard value), *Unsaved* (in the draft), *From
  environment · NAME* (the input is read-only and there is no Reset), *Applies after a restart*. Every control sits in
  one column of the same width. A hop-color row names the incident level it relates to and its value, with "Go to that
  setting".
- **Nothing applies until Save.** The Save button counts the changes ("Save 3 changes"); Cancel, the close button and
  Escape ask before throwing a draft away. Every number is checked against its range in the browser (the message
  appears under the field and Save stays off) and again by the server 0.4 s after the last edit (messages and amber
  warnings land under the field they name). Saving sends the revision the page loaded; if a part you touched changed
  elsewhere the server answers 409, the page keeps theirs, tells you which, and keeps the rest of your draft.
- **Preview** (shown when the draft changes an outage or alert rule) replays the last N days of stored samples with the
  draft rules: "4 incidents instead of 9 · 2 alerts instead of 6 · recorded: 11", with the notes and a short list of the
  incidents that would no longer open or would newly open. It runs on demand and is marked out of date after a further
  edit.
- **Secrets** (the ntfy topic and token, a webhook's address, a header's value) are write-only: the row says "Set ·
  host" or "Not set" and offers Replace and Remove. The text you type goes to the server in the Save and is never shown
  again, kept in a version or exported.
- **Lists:** webhooks (Alerts), host app checks (Devices and services) and DNS lookups (Probing) are cards with Add,
  Remove and, where order matters, Move up/down. A saved item's id is fixed. They apply without a restart. A device
  card is named by its display name, else by what it is and its address ("Xfinity gateway · 192.0.2.1").
- **Versions** lists the last 50 Saves; **Load** puts one in the page as a draft and Save restores it. **Export** downloads
  the settings as YAML (without secrets; viewers do not get it) and **Import** reads such a file, or a config.yaml, into
  a draft with notes on what it left out.
- **Display** has the **time zone** (quiet hours and the daily route check follow it, and the reports' days and hours
  unless it is UTC; it starts as the server's zone, which a container takes from `TZ`, see
  [Install](install.md#container)). Zones are listed by place ("New York (America/New_York)"), this browser's and the
  server's first; UTC is "UTC"; when the setting is not this browser's zone, "Use this browser's zone" puts it in the
  draft. **Layout for new browsers** is one row for `dashboard.order` and `dashboard.hidden`: the layout in words (the
  panels' titles), "Use this browser's layout", which writes this browser's panel order and hidden panels into the
  draft, and Reset to standard. A browser's own Layout choice wins over it (the Layout dialog links to the row).
- **View only.** Without the password (when Sign-in's "Others may look, but not change anything" allows viewing) the
  page shows a "View only" bar, every input is disabled and every changing control carries `data-needs-edit`; signing
  in turns them on.
- **Sign-in.** Who can open the dashboard and the password ([security.md](security.md#who-can-open-the-dashboard)).
  Unlike the rest of the page, Set a password, Change, Show and Remove apply at once, without Save (as a device's
  password does); the viewers switch below them is an ordinary setting.
- **The first-run question.** On a fresh install (no password from anywhere), the first visit opens "Who can open this
  dashboard?" over the page (`dialogs/access-setup`), once.

The help texts of the panels (hop colors, the busy level, router and DOCSIS levels) are functions of the settings values
(`js/settings-values.js`, GET `/api/settings/values`), so they quote what the page uses now. The latency panel opens on
`display.default_latency_range`.

## Mobile tabs

At 720 px and below a bottom bar (`static/js/tabs.js`) shows one group at a time: **Live** (path health, latency, hop
details, uptime), **Network** (router, host services), **Line** and **Incidents** (incident log, when it breaks,
reliability). A panel names its tab in its registry entry (`tab:`). The tab is kept in `location.hash`, so back and
reload return to it; a tab with no available, unhidden panel is left out; a dot marks a tab whose cards are not fine.
Wider than 720 px there is no bar and every shown panel appears.

## Dialogs

- Owned by a panel (extra `*.html` in its folder): **Local network** (SVG topology, filter chips, clients table) and
  **Router history** (router), **Channels** with a per-channel history (line, DOCSIS only), **Inspector** (incidents).
- Standalone (`static/dialogs/<id>/`): **add-target**, **ai-pack**, **alerts**, **device-setup** (credentials and a test poll for a
  device), **dossier** (outage dossier, with its own markdown renderer), **layout**, **settings**, **path-editor** (the probe path:
  Path health's Edit, first-run review, Rediscover, Export / Import), **sign-out**, **target-history**, **trace**.

Dialogs are `div.modal` toggled by `.active` (`openModal` in `util.js`); on a phone they open as a bottom sheet. Their
help texts and labels live in their own folder.

A standalone dialog loads its code when it is first opened (all but **alerts**, which watches the diagnosis from the
start): its folder has a `dialog.json` such as `{"lazy": {"actions": ["openSettings"], "modals": [], "hash": "settings"}}`
naming what opens it (click actions, modals the generic `openModal` action opens, a location hash). `js/panels/index.js`
imports the module the first time one of those is used, and ahead of the click when the pointer or focus reaches the
control. `tests/test_frontend.py` checks that every way into a lazy dialog is named. The styles and markup stay in the page.

The incident list (`panels/incidents/list.js`) loads the newest 50 incidents, and older ones in pages of 50 when the
7-day timeline, a search, the last page, a time window opened from another panel or a custom chart range needs them, up
to 500 (`GET /api/incidents?limit=&offset=`).

A hidden tab is not drawn: `js/live.js` keeps only the newest message and publishes it once when the tab shows again
(then `tab:resumed` makes the live chart fetch the minute it missed). A subscriber that must see every change of its
topic meanwhile passes `{ every: true }` to `bus.subscribe` (the alerts, the device feed).

## States

- **Connecting**: gray pill, "Connecting" indicator, skeleton nodes in the chain.
- **Disconnected**: when the socket closes, `body` and the header get `data-conn="lost"`: the pill goes gray, cards dim
  and a banner under the header says whether data was ever received ("Connection lost. Showing the last data" or
  "Cannot reach pengumon"). The socket retries every 2 s.
- **Stale**: device cards (`static/js/device-panel.js`) show a "reading N min old" chip and dim when the last reading is
  older than three poll intervals (at least 2 min); this is re-checked every 30 s. The card also shows "read HH:MM
  · every N".
- **Device errors**: a device whose status is `error` shows "No reading", with an error block saying what to do; an
  unconfigured one shows "Not set up" and a setup button; warning and critical readings use the server's grades (the
  browser never grades a number itself).
- **Busy**: a line load that explains the latency is "Busy" (purple, expected), not a fault.

Status always has an icon and a word as well as a color.

## Theming

Dark first. `static/tokens.css` holds every color and size as a CSS variable; light values apply under
`prefers-color-scheme: light` and again under `[data-theme="light"]`, so the choice in the header's theme menu wins both
ways. Themes (docs/themes.md) are folders under `static/themes/`; the page shows one as `<html data-look>`, and a theme
overrides the tokens. This browser's choices are kept in `localStorage` (`pengumon.look`, `pengumon.theme`,
`pengumon.effects`); with none, Settings > Display and the operating system's setting apply. `static/js/theme-boot.js`
sets them before the first paint and links the one stylesheet of the theme in use (its tokens, skin and card views' styles),
and `static/js/themes.js` runs the rest (the menu, a switch, the effect, the cards' own colors).
Charts are drawn on canvas, so `static/js/chart-theme.js` reads the tokens and every chart is redrawn on a `themechange`.
Rule: no hex literal outside `tokens.css` and the themes' `theme.json`.

## Where things live

All under `src/pengumon/web/static/`:

- `tokens.css`: design tokens (colors, type scale `--fs-*`, spacing `--sp-*`, radii, motion).
- `components.css`: shared components (panel, badge, button, modal, menu, table), tokens only.
- `page.css`, `style.css`: page layout (header, grid, banners), and the app layer (panel groups, modal behavior).
- `js/`: the core and shared library: `main.js` (entry), `live.js` (the socket; one bus topic per key of each
  message), `bus.js`, `layout.js`, `tabs.js`, `chrome.js` (menus), `themes.js` and `theme-boot.js` (themes), `dispatch.js` (delegated actions),
  `devices.js` and `device-feed.js` (devices and features), `device-panel.js`, `help.js`, `settings-values.js` (the live
  settings values for help texts), `util.js`, `state.js`,
  `grades.js`, `path.js`, `path-api.js` (the path and discovery API calls), `session.js` (sends a page whose sign-in ended to the
  sign-in page), `sparkline.js`, `chart-theme.js`.
- `panels/<id>/`, `dialogs/<id>/`: the features.
- `vendor/`: Chart.js. The page is served from `/static/v/<hash>/` where the hash covers the whole tree, so all modules
  are cache-busted together and cached for a year.

Event handlers are delegated: markup carries `data-action`, `data-input`, `data-change` or `data-submit`, and modules
register the handler with `registerActions`, which keeps the CSP.

**Metric help.** An info button (`data-help="<key>"`) opens a popover saying what a number measures and what is
normal. Each folder registers its texts with `registerHelp` in its `help.js`; a key must exist and be used. A text that
quotes a threshold is a function of the settings values (`(values) => string`), so it never goes stale. Use this instead of `title=` (which stays on icon-only buttons).

## Adding a panel

A panel is one folder, `static/panels/<id>/`; no shared file names it (the page, the config, the loader and the core
modules find it by discovery in `pengumon/web/ui.py`). Files:

- `panel.json`: `{"order": <number>}`, its default place.
- `panel.html`: exactly one `<section data-panel="<id>">` holding one or more `.panel` cards (`data-size` 4, 8 or 12).
  Any other `*.html` in the folder is a dialog the panel owns, included outside `<main>`.
- `panel.js`: default-exports `{ id, title, tab, icon, core?, available(ctx) | instances(ctx), reason?(ctx),
  prepare?(ctx), mount?(section, ctx) }`. `id` is the folder name; `tab` is `live`, `network`, `line` or `incidents`;
  `available` must exist. Subscribe to the live data with `ctx.bus`.
- `help.js`: its `registerHelp({...})` texts.
- `*.css`: its styles, linked automatically; tokens only.

The folder name becomes a valid `dashboard:` id on its own. Rules enforced by `tests/test_frontend.py`: every folder has
the three files and one section with its own id, and module ids match folder names; core modules name no optional panel
and import only core files; a folder imports only `static/js` and its own files, never another folder; no inline
handlers or scripts; every delegated action in the markup is registered (once); every `getElementById` literal exists;
no vendor names or fixed hosts; device templates carry no ids (one card per device); help keys exist and are used;
tabs are named consistently. Add a smoke step to `tests/ui/smoke.mjs` for what a person does with the panel.

## Ask AI (the troubleshooting pack)

The sparkle button in the header, the inspector's **Ask AI** (that incident, with an hour on each side) and a button in the
dossier open one dialog that shows the pack (`GET /api/ai-pack`) as Markdown, to copy into any chat assistant. Pengumon calls
no AI itself. What the pack holds and what each level shares: [ai-pack.md](ai-pack.md).
- **What to cover:** the past hour, 24 hours or 7 days, or the incident the dialog was opened from.
- **How long:** Short, or With more examples.
- **How much to share:** Full (the default every time it opens), Vague or Anonymized; each says in the dialog what it includes. Anyone
  who can view the dashboard may use every level.
- **Preview** is exactly what Copy and Download .md give, with a token estimate (about characters / 4) and the modules included.
  While a new preview loads, the earlier text stays, dimmed, and Copy and Download are off so a pack made for other choices is
  never copied. A change waits 0.6 s before it asks (the route allows 6 a minute); a busy answer (429) retries by itself.
  The pack's times are in the zone set in Settings, else in the browser's own zone (the dialog sends it), else UTC.
- **Who is who** (folded away) is the key to the roles; it is never in the pack.
- Messages (copied, busy, errors, the plain-HTTP download note) are in the dialog's footer, always in view.
