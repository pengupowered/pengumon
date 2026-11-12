# Themes

A theme sets Pengumon's colors, and can also change its type, sizes and shapes and add a slow animated effect. Every
browser picks one in the header's theme menu (the palette button); **Settings > Display > Theme** is the one a browser
starts with. This page is the reference for adding a theme: a theme is one folder, and nothing else in the code names
it.

- [The folder](#the-folder)
- [theme.json](#themejson)
- [The tokens](#the-tokens)
- [Skins](#skins) ([page hooks](#page-hooks-a-skin-may-rely-on))
- [Effects](#effects)
- [Chart style](#chart-style)
- [Restyling a card](#restyling-a-card)
- [Drawing a card](#drawing-a-card)
- [Converting a card to a model and views](#converting-a-card-to-a-model-and-views)
- [Checks every theme must pass](#checks-every-theme-must-pass)
- [How the page applies a theme](#how-the-page-applies-a-theme)
- [Custom themes](#custom-themes)
- [Card colors](#card-colors)

## The folder

```
src/pengumon/web/static/themes/<id>/
  theme.json   required: names the theme and gives its tokens
  skin.css     optional: shape and texture rules, scoped to this theme
  effect.js    optional: the animated effect (named by "effect" in theme.json)
  panels/<panel id>.js   optional: the theme's own view of that card (see Drawing a card)
  panels/<panel id>.css  optional: its styles, scoped to this theme; alone (no .js), the theme's restyling of the
                         card's standard view (see Restyling a card)
```

The id is the folder's name: lower-case letters, digits and dashes, at most 40 characters, not starting with
`custom-` (that prefix is for the owner's own themes). At start, `pengumon.web.themes` reads every folder; the page and
`GET /api/themes` list the themes in this order: `default` first, then by name. A folder whose `theme.json` breaks a rule
is left out and logged, and `tests/test_themes.py` fails on it.

Names are generic: describe the look ("Starship console", "Digital rain"), never a film, series, game or brand, and do not
copy logos, fonts or trade dress. A published palette under a free license (Nord, Dracula, Solarized; MIT) may keep its
name; say so in the description.

Fonts: themes use the system's font stacks only. The page's Content-Security-Policy (`font-src 'self'`) loads no web fonts,
and no font files are added to the repository without the owner's approval.

## theme.json

```json
{
  "id": "nord",
  "name": "Nord",
  "description": "The Nord palette: arctic blue-grays with frost and aurora accents. Dark and light.",
  "modes": ["dark", "light"],
  "tokens": {
    "dark": { "--bg": "#242933", "--surface-1": "#2e3440", "...": "..." },
    "light": { "--bg": "#e5e9f0", "...": "..." }
  },
  "effect": null,
  "tags": ["palette", "calm"]
}
```

| Field | What it is |
|---|---|
| `id` | the folder's name |
| `name` | shown in the theme menu, Settings and Layout; 1 to 40 characters |
| `description` | one sentence for Settings > Display; at most 200 characters |
| `modes` | `["dark"]`, `["light"]` or `["dark", "light"]`. A theme with one mode forces it: the menu's light/dark choice is off and says why |
| `tokens` | one object per mode in `modes`: token name to value (see below). The `default` theme gives `{}`: its tokens are `tokens.css` |
| `effect` | `null`, or `{"id": "<file stem>", "default_on": true}`: the effect is `<id>.js` in the folder (`effect` for `effect.js`). `default_on: false` runs it only for a browser that turns effects on |
| `tags` | up to 8 short words (`palette`, `skin`, `effect`, ...), for lists |
| `chart` | optional: the shape of the canvas charts, see [Chart style](#chart-style) |

No other field is allowed.

## The tokens

The tokens are the CSS custom properties of `static/tokens.css`; a theme gives new values for some of them. Everything on
the page, canvas charts included, is drawn from these tokens, so a theme that sets them changes the whole page.

**Required in every mode the theme has: every color token.** These are the tokens whose value in `tokens.css` is a
color:

| Group | Tokens |
|---|---|
| Backgrounds and lines | `--bg`, `--surface-1` (cards), `--surface-2` (insets), `--surface-3` (hover, chips), `--line`, `--line-strong`, `--scrim` (behind dialogs) |
| Text | `--ink-1`, `--ink-2`, `--ink-3`, `--ink-4` |
| Accent | `--accent`, `--accent-bg`, `--on-accent`, `--focus` |
| Status | `--good`, `--good-bg`, `--warn`, `--warn-bg`, `--warn-fill`, `--serious`, `--serious-bg`, `--crit`, `--crit-bg`, `--busy`, `--busy-bg`, `--unknown`, `--unknown-bg`, `--nodata` |
| Chart lines (series) | `--s1` ... `--s8`, in this order (a target without a color of its own takes the next one) |
| Loss heat | `--q0` (none) ... `--q4` (most) |

Color values: a token whose value in `tokens.css` is `#rrggbb` must stay `#rrggbb` (canvas code adds an alpha to it:
`#3987e5` + `26`); the others (`--scrim`, the `-bg` tints, `--accent-bg`) may also be `#rrggbbaa` or `rgba(r, g, b, a)`.

The status colors mean the same in every theme: good (healthy), warn, serious, crit (outage), busy (expected: a busy
line, maintenance), unknown (no data yet). Never reuse a status color for something else, and remember the page always
shows a status with an icon and a word too.

**Optional:**

| Tokens | Values |
|---|---|
| `--font`, `--mono` | a system font stack: letters, digits, spaces, commas, quotes and dashes (`ui-monospace, "SF Mono", Menlo, monospace`) |
| `--fs-xs` ... `--fs-2xl` | text sizes in px, each within a range (`--fs-md` 12 to 20 px) |
| `--r-xs`, `--r-sm`, `--r-md`, `--r-lg`, `--r-pill` | corner radii in px |
| `--sp-1` ... `--sp-10`, `--page-max`, `--tabbar-h` | spacing and widths in px, within ranges |
| `--fw-medium`, `--fw-semi`, `--fw-bold` | font weights 100 to 900 |
| `--t-fast`, `--t-med`, `--ease` | motion: durations in ms (0 to 1000), a `cubic-bezier(...)` |
| `--shadow-pop`, `--shadow-sheet`, `--glow-warn`, `--glow-crit` | shadows: no `;`, braces, `url()` or backslashes |

The layers (`--z-*`) cannot be changed. The ranges are in `pengumon/web/themes.py` (`SIZE_RANGES`).

## Skins

`skin.css` changes shapes and textures: borders, glows, letter case, background patterns. Rules:

- Every selector starts with `:root[data-look="<id>"]`, inside `@media`, `@supports` and `@container` too, so a skin
  never touches another theme. `@keyframes` are allowed.
- No `@import`, no `@font-face`, no `url()` to another site.
- Colors come from the tokens (`var(--accent)`, `color-mix(in srgb, var(--good) 40%, transparent)`), not new literals.
- Keep text readable and the focus ring visible (`--focus`); do not hide anything, do not move controls, and keep 390 px
  phone width without sideways scrolling.
- Motion in a skin (an animation) stops under `@media (prefers-reduced-motion: reduce)`.

What the skin and view checks do not catch: they are a lint for the theme folders that ship in this repository (they are
reviewed like any code), not a security boundary. They read selectors and look for words, so a determined author gets past
them (a value built from pieces, a `url()` hidden in a custom property, a view that reaches `fetch` by a computed name).
The checks refuse nested rules inside a scoped rule, CSS escapes in `url(`, `@import`, `src()` and `image-set()` to
another site, and `import()` in a view; nothing more is promised. For the same reason, they are not applied to user-supplied
files: a custom theme cannot bring a skin or a view at all, only values that the server checks one by one.

A theme's skin, its token blocks and its card views' stylesheets are one generated stylesheet,
`/static/v/<tree>/themes/<id>/theme.css` (`pengumon.web.themes.look_css`: tokens, then skin, then views), and the page
links it only while the theme is in use ([How the page applies a theme](#how-the-page-applies-a-theme)). It is served from
the theme's folder, so a skin's `url()` to a file there still resolves; do not add a file named `theme.css`.

### Page hooks a skin may rely on

A skin that draws across the page (Starship's frame: a rail down every card, sweeping into its header bar; the page header stays fixed while the page scrolls, so a frame drawn on it would slide over the cards) styles
the page's own structure, not only the cards' insides. These are its hooks; the page template, the card markup and
`js/layout.js` keep them, and `tests/test_themes.py` (`test_the_page_keeps_the_hooks_a_skin_relies_on`) fails when one
disappears or moves:

| Part | Hooks |
|---|---|
| The header | `header.topbar` (its `::before` is the header's background) with `.tb-inner` as its direct child (`::before` and `::after` free for a skin) |
| The effect's layer and the glow | `.fx-layer` (the effect draws in it, behind everything unless a skin raises it), `.glow` |
| The card grid | `main.page`: only `section[data-panel]`s as its children, each holding its cards (`article.panel`, also inside a `template` for the cards made per device) as its own children; `.page::after` is free for a skin |
| A card | `article.panel` with `[data-status]` while it has a status; its own children: `header.panel-h` first (with `.panel-t`, `.panel-m`, `.panel-tools`), then `.panel-b`, then `.panel-f` where it has a foot. `.panel-tools` is in every card's header, the card's menu button (`.panel-menu-btn`) its last child (`js/layout.js` `addMenuButton`) |
| A dialog | `.modal-content` (a `div.modal` dialog) or `dialog.modal`, with one `.modal-h` (`.modal-t`, `.modal-m`, `.tools`; `.m-close` its own child), `.modal-b`, `.modal-f` |
| The phone tab bar | `nav.tabbar` (`button[aria-selected]`) |

The page has no element of its own for a theme to draw on: a skin draws with these parts' backgrounds, borders and
`::before` / `::after`.

## Effects

`effect.js` is an ES module:

```js
export default {
  start(layer, ctx) {
    const canvas = document.createElement('canvas');
    layer.append(canvas);
    const off = ctx.onFrame((now, dt) => { /* draw one frame */ });
    return () => { off(); canvas.remove(); }; // stop: undo everything start did
  }
};
```

- `layer` is `#fxLayer`: a full-viewport element behind the page (`position: fixed`, `pointer-events: none`,
  `aria-hidden`). Draw into it only.
- `ctx.tokens()` gives the current tokens by name (`ctx.tokens()['--accent']`), read at the moment you ask: read them in
  each frame and the effect follows a light/dark switch.
- `ctx.fps` is the cap, 15 frames a second.
- `ctx.onFrame(cb, fps)` registers `cb(now, dt)` with the engine's one frame loop and returns a function that
  unregisters it. The loop runs at `fps` (optional; 15 at most, the default) and is paused while the tab is hidden. Ask
  for as few frames as the look allows: every frame costs the whole page a style, paint and composite pass (about 4 to
  5 ms of main thread in the test browser), whatever the effect draws. **Effects never call `requestAnimationFrame` or `setInterval`
  themselves** (the test checks).
- `start` returns `stop`; it must remove listeners (a `resize` handler, for example) and what it added to the layer.

The engine (`static/js/themes.js`) runs an effect only while all of these hold: the theme has one, effects are on
(Settings > Display > Animated effects, and this browser's choice in the theme menu), the system does not ask for reduced
motion (`prefers-reduced-motion: reduce`), and the tab is visible. It stops the effect when any of them changes.

Budget: under 3 % of one core in the headless browser of the UI tests, measured as in
[Measuring an effect](#measuring-an-effect). What keeps an effect cheap: few frames a second (each frame costs the page a
style, paint and composite pass whatever the effect draws), a canvas only as large as it must be, the canvas on a layer
of its own (`will-change: transform` in the skin, so a frame repaints the canvas and not the cards over it), its font set
once (setting a canvas font costs a style lookup), and a fade drawn into the canvas instead of a CSS `mask-image` (a mask
over a changing canvas costs a compositing pass each frame: 15 frames with a mask took about 30 % of a core). Starship
console (2 frames a second), Amber CRT and Synthwave (3) stay within it. **Digital rain is the one exception, by
choice:** its rain falls over the whole page behind see-through cards (so that the rain can be seen at all), at 4
frames a second, and costs about 3.5 % of a core on the main thread in the test browser (below). A browser that
composites on a graphics card pays less; anyone can turn effects off in the theme menu.

### Measuring an effect

Run the page with the theme picked and reduced motion off, then 30 s with the effect on and 30 s with it off, and
compare the main thread's busy time (Chrome's `Performance.getMetrics`, `TaskDuration`) and the CPU time of every browser
process (`/proc/<pid>/stat`), both as the effect's increase over the same page without it
measured in the headless Chromium of the UI test image at 1440 x 1000 on the demo network's live page. 0.24.0:

| Effect | Main thread | Every browser process |
|---|---|---|
| Digital rain, whole page, 4 fps, cards 50 % opaque | +3.5 % of one core | +5.7 % |
| Starship console, 2 fps | +1.2 to 1.9 % | +0.1 to 1.4 % |
| Amber CRT, 3 fps | +1.5 to 1.6 % | +1.8 to 2.5 % |
| Synthwave, 3 fps, 240 px canvas | +0.9 to 1.1 % | +1.6 to 2.3 % |

The whole-browser figure varies by a few percent between runs. The page itself costs more than any effect there: the
header's background blur is about a quarter of a core in software compositing.

## Chart style

`"chart"` in theme.json changes the shape of every Chart.js chart while the theme is shown (the latency chart, the
history dialogs, the device charts), not only its colors. Every field is optional; a theme without `chart` gets the
charts as their modules draw them.

```json
"chart": {"line_width": 3, "curve": "straight", "grid": false, "points": 2, "fill": true, "spark": "bars"}
```

| Field | Values | What it does |
|---|---|---|
| `line_width` | 0.5 to 6 | the width in px of a chart's main lines (the charts' own: 2); thinner lines (one per target, the WAN download) keep their proportion |
| `curve` | `smooth`, `straight`, `stepped` | smooth: the chart's own curve; straight: straight segments between readings; stepped: steps (a dataset with `ownCurve: true` keeps its own curve, see below) |
| `grid` | true, false | false: no grid lines in the plot area |
| `points` | 0 to 6 | a dot of this radius on every reading (0: none, the charts' own) |
| `fill` | true, false | true: the area under each series line is washed with its color (a band or an area a chart fills itself keeps its own) |
| `spark` | `line`, `bars` | the sparklines (a path node's and a hop's last probes, `static/js/sparkline.js`): a line (the default), or a bar per probe as tall as its round trip, a lost probe a full-height bar in the outage color |

`check_theme` (`pengumon/web/themes.py`, `CHART_STYLE`) refuses an unknown field or a value out of range; a custom theme has
no chart style. The page reads the style of the theme it shows from `<html data-looks>` (`chart`), and
`static/js/chart-theme.js` applies it: a Chart.js plugin registered once (`themeChartStyle`) sets, before every update of
every chart, each line dataset's width, tension, `stepped`, point radius and fill from the dataset's own values (kept
the first time it sees the dataset) and the theme's style, and hides the grid lines when `grid` is false. Modules keep
giving their datasets their own widths and curves and never ask for the style (a dataset whose readings are far apart, like the Speed card's one point per test, sets `ownCurve: true` and keeps its own tension and `stepped` whatever `curve` says; its width, points, fill and the grid still follow the style); a theme change redraws every chart
(`applyChartTheme`), so the new style applies at once. The sparklines are not Chart.js charts: `sparkline.js` reads
`spark` (`chartStyle()`) each time it draws one, and the cards draw again on a theme change. The router's last hour keeps
its shape.

## Drawing a card

A card's look can change more than its colors: a theme may draw a card its own way, as long as everything the card
shows and does is still there. Every card that is drawn by views is split in two:

- its **model** (`panels/<id>/model.js`; the latency card's is `charts.js`): fetching, timers, state, the actions, and
  the **view model**: plain data with every value already formatted;
- its **views**, which draw the card's body from the view model: the **standard view** (`panels/<id>/view.js`) is the
  look every theme gets, and a built-in theme may add its own, `themes/<theme id>/panels/<panel id>.js` (and `.css`).

A card a theme does not draw keeps the standard view in that theme's colors and skin (and in its own shapes, where the
theme [restyles](#restyling-a-card) it). The engine is
`static/js/cards.js`; the three reference cards are diagnosis (Path health), services (Host services) and latency.

### The view model

What the model hands its card (`card.update(vm)`) whenever its data changed:

- **Values as the card shows them**: text with its unit and wording (`"12 ms"`, `"checked 12s ago"`, `"0.0%"`), never a
  raw number a view would have to format. A view may lay a value out differently (a pill label over it, upper case by
  CSS) but shows the model's text.
- **Statuses** as `{level, word, icon}`: `level` is one of the status tokens (`good`, `warn`, `serious`, `crit`, `busy`,
  `unknown`), `word` what the card says, `icon` the sprite's name. A status is always icon and word, never color alone.
- **Actions** as `{action, arg, arg2, label, icon, needsEdit, expanded}`: `action` is a name the card's model
  registered (`registerActions`), `label` the accessible name. **Links** as `{href, label, icon}` (already checked to be
  http or https).
- **State that must survive a theme switch** lives in the model, not in a view: which rows are open, the chosen line
  mode. A view keeps only what it can lose (a measured width).
- A part of the view model is a new object only when it changed, so a view can skip work by identity
  (`vm.path !== prev.path`). It may carry functions for values a view asks for on demand (a tooltip's lines).

The three references: services `{rows: [{id, status, name, reason, checkUrl, ms, description, facts: [{key, text}], open,
copy, history, outages}], summary, status}`; diagnosis `{verdict: {level, word, title, text, inspect}, banner: {level,
icon, title, text, progress, actions} | null, path: {hostCount, nodes, folded(), none, extras} | null, busyLegend}` (a
node: `{key, label, name, nameTitle, level, word, statusIcon, value: {num, unit} | {word}, loss, jitter, quiet, spark,
link: {text, title, level, broken}, target, group, aria}`); latency `{labels, times, lines: [{id, kind, label, color:
{token} | {value}, data}], band, wan, loss, incidents, legend: [{key, kind, color, label, sub, title}], note, modes:
[{action, arg, label, title, pressed}], memberLines(id, i), lossLabel(i, pct)}`.

### A view

```js
export default {
  mount(root, ctx) {
    // draw the empty body into root; return what updates and undoes it
    return {
      update(vm, prev) { /* change what differs from prev */ },
      unmount() { /* undo what mount did outside root: observers, listeners, charts */ }
    };
  }
};
```

- `root` is the card's body (`.panel-b`): empty for a theme view, the card's `panel.html` body markup for the standard
  view. A view draws inside `root` only and finds its elements there (`root.querySelector`), never by a document id
  (the parity test draws a second copy of each view, with the ids taken out), and a theme view sets no ids ([Finding a
  card's parts](#finding-a-cards-parts-tests-and-tools-no-ids-in-a-theme-view)).
- `update(vm, prev)` runs for every new view model (`prev`: the one before, `undefined` after mount) and again after
  every theme change with the same view model (a view that reads tokens, a canvas chart, reads them there). It changes
  only what changed: live data comes every second on some cards, and a redraw must keep the keyboard focus, the scroll
  position, open rows and the elements a person is pointing at. `kit.list` keeps a list's unchanged rows as they are.
- `unmount()` undoes what `mount` did outside `root`; the engine empties `root` itself.
- `ctx`:

| Field | What it is |
|---|---|
| `ctx.panel` | the panel id |
| `ctx.look` | the theme it draws for (`standard` for the standard view) |
| `ctx.card` | the card's `<article class="panel">`, its frame included: read it (its width), do not change it |
| `ctx.std` | the card's standard view module; `ctx.std.parts` are its part renderers, to reuse (below) |
| `ctx.kit` | helpers: `el(tag, class, text)`, `text(el, value)` and `html(el, markup)` (written only when they differ), `mark(el, key, level)`, `status(el, level, word, {icon, iconClass, wordClass})` (writes a status into `el` as icon + word, with `data-st`; only when it changed), `action(descriptor, {className, text})` (a button or link for an action descriptor; `text: false` for an icon button), `list(container, items, {key, render, sig})`, `icon(name)` (markup), `iconEl(name)`, `escapeHtml`, `STATUS` |
| `ctx.vm()` | the view model now |

**Part renderers.** A standard view exports `parts`, each a small view of one part of the card (`(element, ...) ->
{update(...), unmount()}`) or a function that draws one element; a theme view uses them for what it does not draw
itself. Services: `row(r)`, `rows(list)`, `facts(box, facts)`, `history(r)`. Diagnosis: `verdict(box)`, `notice(box)`,
`chain(list)` (the whole chain, with its measuring and folding), `extras(box)`, `legend(foot)`, `nodeShell(n)`,
`paintNode(...)`, and for a view that lays the card out its own way (the hero themes' views use them; each names its
elements with the theme's class prefix, `ac` gives `.ac-verdict`, `.ac-hop`, ...): `verdictLine(parent, prefix)` (the
verdict: status tag, title, sentence in `.<p>-vwords`, Inspect), `hopFields(prefix, hopClass)` (`make()` gives a hop's
button and its fields for the view to place, `patch(fields, node)` writes a node into them), `extraChips(parent,
prefix, heading)` (the other targets as chips) and `blockTrace(values, count)` (the last probes as block characters).
Latency: `charts(latencyCanvas, lossCanvas)` (both Chart.js charts, tooltips and line-end names included),
`legend(box, note)`, `legendHtml(items)`, `legendList(box, prefix, arrange)` (the legend as entries a view arranges its
own way: `arrange(parts, item)` places the entry's key, name, newest reading and members), `modes(group)`,
`datasets(vm, element)`. Incidents: `timeline(box)`, `rows(tbody)`, `row(r)`, `windowBar(bar)` (a view's window bar carries `data-role="window"` and `tabindex="-1"`: the model focuses it when another panel opens a time window). When it breaks (heat): `grid(box)`. Reliability (report): `hero`, `days`, `causes`,
`hops` (each `(box, vm)` writes one part). Uptime (strips): `grid(box)`. Hop details (matrix): `rows(tbody)`, `build(r)`, `patch(tr, r)` (a row and its
patch, sparkline included). The device cards expose one part per capability, so a theme can draw some and reuse the rest, and a capability a driver offers later is a new part of
the view model, not a theme change. Router (`panels/router/view.js`; view model `{error, dim, notice, wan, system, clients,
mesh}`, a part null when the device lacks it): `wan(box)`, `system(box)`, `clients(box)`, `mesh(box)`, `meter(box)`,
`chart(box)`, `node(n)`, `notice(box)`, `error(box)`. Line (`panels/line/view.js`; `{error, dim, note, readings}`, where
`readings` is the technology adapter's `{gauges: [...], errors, channels}`): `gauge(box)` (one per entry of `gauges`),
`errors(box)`, `channels(box)`, `readings(box)` (all of them), `note(box)`, `error(box)`. A part owns the markup inside the
box it is given and puts `data-item` on it where its marks need a scope. `fixture.variants` (functions) gives a card
more fixtures for the parity step, e.g. a driver with the fewest capabilities (router: system only; line: a technology
with no adapter).

**The frame.** A card's header and footer (`panel.html`: icon, title, the header's summary and tools, the footer) are
the same in every view: the model keeps their values, the help buttons stay where they are, and the Layout menu button is
the page's. A theme restyles them with CSS while its view is shown: the engine puts `data-view="<theme id>"` on the
card's `<article>` and on its body (`standard` for the standard view, `loading` while a theme view loads), so
`:root[data-look="amber-crt"] .panel[data-view="amber-crt"] > .panel-h` is the header of a card Amber CRT draws, and a
standard view standing in for a broken theme view keeps its own look.

**What a view must keep** (the standard view's accessibility, too): headings in order under the card's `h2`, a label
for every control, everything reachable and usable by keyboard with the focus ring visible, no sideways scroll at 390 px,
text inserted with `textContent` or `escapeHtml` only (eslint and `tests/test_frontend.py` cover `themes/**`), colors
from the tokens. A view fetches nothing, starts no timer and stores nothing (no `fetch`, `XMLHttpRequest`,
`WebSocket`, `setInterval`, `requestAnimationFrame`, `localStorage`, `sessionStorage`, `eval`: the folder test refuses
them); the model does that.

### The marks: no data lost

Every value, status, action and help the standard view shows is marked, and a theme view of the card must show the
same marks. A theme view may add things; it may not leave one out.

| Mark | On | Its value |
|---|---|---|
| `data-v="<key>"` | an element that shows a value | its `aria-label`, else its text, else its `title` (whitespace folded); compared as text |
| `data-st="<level>"` | the element that holds a status's icon and word (with `data-v` for the word, when it has one) | the level, and that it has an icon (`svg`) |
| `data-item="<key>"` | a row (a service, a path node, a legend item) | names the marks inside it: `fx_up/ms`, `Router:fx_router/delta` |
| `data-action` (`data-arg`, `data-arg2`, `data-needs-edit`) | a control | the action with its arguments and whether it needs the sign-in to edit |
| `data-help` | a help button | its key |

Marks inside a `hidden` element do not count. `cards.js` `collectMarks(root)` reads them and `compareMarks(ours,
theirs)` says, in words, what a theme view lacks. The test (`tests/ui/steps/core.mjs`, "card views: every theme's view
of a card ...") runs in the browser: for each card on the page with a **fixture** (the model's `fixture()`, given to
`createCard`: a view model made by the model's own code from fixed, made-up data: 192.0.2.x, example.com) it draws the
standard view and every theme's view of that card apart from the page from that one view model (`drawApart`) and fails
on any missing or different mark, on a theme view of a card that has no fixture, and on a fixture whose standard view
draws too few marks to mean anything (fewer than three values or no action). So a card that gains a value later fails the test until every theme view shows it
(or the theme view is removed).

### Finding a card's parts (tests and tools): no ids in a theme view

A theme view sets no element ids. The ids in a card's `panel.html` belong to its frame and its standard view (the steps
that test the standard view use them); the model reads only the frame's ids (the header's summary, the footer, the
`<section>`), never one inside the body. Code outside the card that must find a part in whatever view draws it (the UI
steps, screenshot and measuring scripts) finds it by what the engine and the contract own:

- the card: `[data-panel="<panel id>"]`; its body: `[data-panel="<panel id>"] .panel-b`, where the engine puts
  `data-view="<theme id>"` (or `standard`) once a view is mounted;
- inside it, the marks every view must carry: `[data-item]` rows, `[data-v="<key>"]` values, `[data-st]` statuses and
  `[data-action]` controls;
- `data-role="<name>"` for the one element a model itself must find in the body: the incidents card's window bar,
  `data-role="window"`, which the model focuses when another card opens a time window. A new one is named here.

For example: the path is drawn (the first message came) when `[data-panel=diagnosis] .panel-b [data-item]
[data-action=openTargetHistory]` (a node of the path) exists; the heat grid's hours are `[data-panel=heat] .panel-b
[data-v=tip]`; the latency chart is the line chart among `[data-panel=latency] .panel-b canvas` (`Chart.getChart`).

### How the engine mounts, updates and swaps views

- The model creates its card once it is mounted: `createCard({panel, root, standard, fixture})` (`root`: the card's
  `.panel-b`; one call per card for a panel with several instances, one card per device), then calls `card.update(vm)`
  whenever its data changed (it answers whether the view drew it now: false while the card waits off screen or for its
  view).
- The engine shows the current theme's view of the card (`<html data-looks>`: each built-in theme's `views`, panel id
  to module URL), or the standard view. A theme view is imported only when its theme is in use, once per page
  (`themes.js` `loadView`);
  `js/theme-boot.js` preloads the views of the theme the page starts with (`<link rel="modulepreload">`), so they are
  there when the cards mount. While a card's first view loads, its body is hidden (`data-view="loading"`); a view model
  that comes meanwhile is drawn as soon as it is there.
- On a theme change (`themechange`) a card whose view changes is swapped without a reload: the old view's `unmount`,
  the body emptied (or given back its `panel.html` markup for the standard view), the new view's `mount` and an
  `update` with the latest view model. The control that had the focus gets it back in the new view (the same action and
  arguments, or the same place), and the scroll position stays. A card whose view does not change only draws again.
- **Not drawn off screen (a per-card choice)**: a card that updates often and sits low on the page can pass
  `offscreen: 'defer'` to `createCard`. While its body is not on screen, nor within 200 px of it (an
  `IntersectionObserver`; a body that is not shown at all, as a card on another phone tab, is off screen), it keeps
  only its latest view model, and its view's `update` runs once, with that one, when it comes on screen (`prev` is the
  view model it drew last). It is drawn at once until it has been drawn once (so it has its size and rows from the
  start, and the page does not grow when it scrolls in), while it holds the focus, and before the page is printed.
  The model still gets every message, so its data is right; only the drawing waits. Hop details (matrix: a message
  every second, the last card), Path health (diagnosis) and the latency chart do this; every other card draws every view
  model wherever it is.
- **Run-time fallback**: a theme view that throws in `mount` or `update`, or that cannot be loaded, is replaced by the
  standard view for that card for the rest of the page's life, with one warning in the console (`A theme's card view
  failed, ...`). A standard view that throws is an error, as before.

### How a theme declares views

By file presence, like a skin: `themes/<id>/panels/<panel id>.js` is that theme's view of the card `<panel id>`, and
`panels/<panel id>.css` its styles (alone, a [restyle](#restyling-a-card) of the standard view). `pengumon.web.themes.view_problems` checks every theme folder at start (a broken
folder is left out and logged; `tests/test_themes.py` fails on it): the panel id is a card the page has, the module has
a default export `{mount(root, ctx)}` and none of the forbidden calls above, a stylesheet follows the
[skin rules](#skins) (every selector under `:root[data-look="<id>"]`) and has no `url()` at all. The views' stylesheets
are part of the theme's one stylesheet (after its skin, [Skins](#skins)), loaded only while the theme is in use; scope a view's rules to the
card while the view draws it (`[data-view="<id>"]`). Custom themes have no views (no code from the settings, ever).

### Worked example: Amber CRT's Incidents

`themes/amber-crt/panels/incidents.js` draws the card as a terminal log: a prompt line, the standard week (the
`timeline` part), the window bar, and one line per incident (`when  [STATUS]  what  duration  cause`) where a line opens
the incident's details; an outage of several drops is one line (`r.kind === 'outage'`) whose title button lists its drops
(its drops follow it in `list.rows` as rows of `kind: 'drop'`) and carries a summary, a state and a
maintenance label (`data-v` `summary`, `state`, `maintenance`); `incidents.css` is its look. Only what looks different is its own code; the timeline is the
standard part. In short:

```js
export default {
  mount(root, ctx) {
    const { el, mark, list } = ctx.kit;
    const { timeline } = ctx.std.parts;                        // the standard part renderers
    const box = root.appendChild(el('div', 'ac-inc'));
    const week = timeline(box.appendChild(el('div', 'tl ac-tl'))); // the standard week, in a box of this view
    const log = box.appendChild(el('div', 'ac-log'));
    const line = (r) => {                                      // r: one row of the view model
      const row = el('div', `ac-ln st-${r.status.level}`);
      row.append(mark(el('span', 'ac-when', r.when.text), 'when')); // data-v="when": the parity test compares it
      // ... the status (mark(..., 'status', level) with its icon and word), the title button with data-action /
      // data-arg from r.open, the duration and the cause, each marked as the standard row marks it
      return row;
    };
    return {
      update(vm, prev) {
        week.update(vm, prev);
        list(log, vm.list.rows, { key: (r) => r.id, render: line }); // unchanged lines stay as they are
      },
      unmount() { week.unmount(); }
    };
  }
};
```

## Restyling a card

A theme that keeps a card's layout of parts but gives it its own shapes does not draw the card: it restyles the
standard view with a stylesheet alone, `themes/<theme id>/panels/<panel id>.css` (no `.js`), which follows the
[skin rules](#skins) and is part of the theme's one stylesheet. The card keeps the standard view (`data-view="standard"`),
so every value, action and help is there by construction, and a card that changes is restyled with no change to the
theme. **Starship console** restyles every card this way: its stylesheets are the worked example
(`themes/starship/panels/*.css`; `skin.css` holds the frame, one flowing frame over the whole page built on the
[page hooks](#page-hooks-a-skin-may-rely-on), and the console parts the cards share: labels, pill buttons, status
blocks). **Mission control** restyles them for a wall display: larger figures (sized with container units, so a
wider card shows them larger), the verdict a banner in its status color, quieter captions (`themes/mission-control/`).

A restyle may rely on what each standard view documents as its **hooks** below, and on the marks (`data-v`, `data-st`,
`data-item`, `data-action`, `data-help`), whose names are the card's contract anyway. A standard view keeps these hooks
when it changes, or changes the themes that use them in the same commit (`grep -r '<hook>' static/themes`). Other
class names of a standard view are its own and may change.

| Card | Hooks |
|---|---|
| every card | `.panel` (the frame: `.panel-h`, `.panel-t`, `.panel-m`, `.panel-tools`, `.panel-b`, `.panel-f`); a status as `.st-<level>`, which sets `--st` (its color) and `--st-bg` |
| Path health (`diagnosis`) | `.verdict` (`.badge`, `.verdict-txt` with `h3` and `p`, `.verdict-act`); `.path-banner .result` (its icon first, then the words, then `.btn`s); `.chain` (`.origin` with `.origin-ic`, `.origin-t`, `.origin-m`; per stage a `li.link` (`.wirebox`, `.cut`, `.delta`) and a `li.node-wrap` with `button.node` (`.node-top` with `.node-ic`, `.node-kind`, `.node-st`: the status icon, its word in `title`; `.node-name`, `.node-val`, `.node-meta`: each figure's name in `.lbl`; `.node-spark`); `.chain[data-dense]`); `.path-extra` (`.pe-h`, `.pe` with its icon first); `.hero-foot` (`[data-legend="good|warn|crit|busy|delta"]`, `.grow`) |
| Latency | `.lat-legend .lg` (`.key`, `.key.line` for a line; `b`, `.muted`); `.lat-view` (a `.seg` of buttons); `.lat-note`; `.chart-box.lat-main`, `.chart-box.lat-loss` |
| Speed | `.spd` of `.spd-state`, `.spd-problems` (`.spd-problem`), `.spd-last` (`.spd-figs` with two `.spd-fig`: `.spd-lbl`, `.spd-big` with `b` and `small`, `.spd-plan`; `.spd-meta`; `.spd-why`; `.spd-remark`; `.spd-bloat` with a `.badge`), `.spd-chart` (`.spd-chart-bar` with `.spd-legend` and a `.seg`; `.chart-box.spd-chart-box`), `.spd-lines` (`.spd-line`, `.spd-link`), `.spd-run` (`.spd-run-btn`, `.spd-confirm` with `.spd-confirm-btns`, `.spd-progress`, `.spd-notice`) |
| Host services | `.svc li.svc-row` (`.svc-st`; `.svc-id` with `.svc-name` and `.svc-why` or `.svc-check-url`; `.svc-ms`; `.svc-actions` with `.svc-open` and the buttons; `.svc-more` with the description, `.svc-facts`, `.svc-check-url`, `.svc-history`) |
| Incidents | `.tl` (`.tl-lane` with `.tl-l` and `.tl-track` of `.tl-bar`s, `.exp`, `.ongoing`; `.tl-axis`, `.tl-now`, `.tl-win`); `.inc-window` (its icon first, then the words, the `.chip`); `.inc-list` (a `.dlist`; `tr.incident-row`, `.expected`; `.primary`, `.badge` (`.quiet` once over), `.when`, `.dur`, `.chip` (the cause), `.go`) |
| When it breaks (`heat`) | `.heat` (`.hd` hours, the day names, `.cell` hours with their fill in `--f`, `i.later` hours to come); `.heat-foot .scale-legend` (two `span`s around five `i`s), `.fine`; `.insight` (an icon, then the sentence) |
| Hop details (`matrix`) | `.hops` (a `.dlist`): `td.lead` (`.primary` the short name, `.sub`), `.chip` (the loss; `.chip.st` when it has a status), `.range` (`i` the band, `b` the average), `.range-n`, `[data-f="last"]` (`.ink-crit`, `.ink-muted`), `.sparkline` |
| Uptime (`strips`) | `.strips` (one grid: `.s-hd`, `.s-hd.r`; per target `.s-name` (`span`, `small`), `.strip` of `.cell`s (`.w`, `.c`, `.b`, `.n`) or `.strip-quiet`, `.s-pct`; then `.s-axis`); `.strip-legend` (`.key`, `.grow`) |
| Reliability (`report`) | `.rep-grid` (`.rep-hero` with `.stat`s and `.hero-num`, `.rep-mini`; `.rep-days` with `.rb-t`, `.days` of `.day`s (`.nodata`; `i` the bar, `span` the letter), `.rep-note`; `.rep-causes` with `.rb-t`, `.causes li` (`.cbar i`, `b`)); `.rep-hops-box` (`summary`, `.rep-hops`, a `.dlist`) |
| Internet use (`traffic`) | `.trf` of `.trf-state`, `.trf-notices` (`.trf-notice` with its status first, `.trf-reason`; `.trf-stale`), `.trf-sums` (`.trf-totals`, `.trf-range`, `.trf-busiest`), `.trf-chart` (`.trf-chart-bar` with `.trf-legend` of `.trf-key` (`.gap` for a hatched one) and a `.seg`; `.chart-box.trf-chart-box`; `.trf-notes` of `.trf-note`), `.trf-cols` of two `.trf-block`s (`.trf-devices`, `.trf-apps`), each a `h3.rb-t` (`.rb-n`, `.muted`) over `.trf-rows` of `.trf-row` (a device: a `button` with its icon, `.trf-who` (`.trf-name`, `.trf-sub`, `.trf-app`), `.trf-figs` (`.trf-down`, `.trf-up`) and `.trf-bar` with an `i` whose width is the share; an app: `.trf-row.app`), `.trf-empty`, `.trf-more` |
| Router | `.rgrid` (`.n<count>`) of `.rblock`s (`.wan`, `.sys`, `.clients`, `.mesh`), each titled `h3.rb-t` with its name in `.rb-n`; `.meter` (`.lbl`, `.val` with `[data-f="v"]`, `.track` with `.fill`; the share in `--v`, the level's color in `--mc`); `.sysfacts` of `li`s (`.sf-l`, `.sf-v`, `.sf-s` with a `.sf-st` status; `.st-warn` / `.st-crit` on the `li`), `.sysevents` (`h4.rb-s`, a `ul` of `li`s with `.ev-w`, `.ev-x`); `.wan-chart`; `.big-count`, `.stackbar` (`i` with `--c`), `.legend`; `.nodes li` (`.st-warn`; `.n-ic`, `.n-id`, `.n-st`, `button.n-c` with `b` and `i`); `.cap-note`; `[data-f="body"].dim` |
| Line | `.line-docsis` of `.gauge`s (`.st-<level>`, `.na`; `.g-name` with `[data-f="ic"]`: the status icon, its word in `data-word`; `[data-f="name"]`, the help; `.g-val`, `.g-bar` (`.g-zone.ok`, `.g-zone.mid`, `.g-spread`, `.g-mark`), `.g-scale`); `.gauge.fec` (`.serious`; `.fec-note`, `.fec-detail`, `.g-bar-free`); `.chan-sum` (`.chan-txt`, `.chan-strip`); `.cap-note`; `[data-f="body"].dim` |

What a restyle may do and may not:

- Lay the parts out its own way (grid areas, `display: contents` on a wrapper, a different order), change every shape,
  and add decoration (`::before`, `::after`).
- Add short labels of its own as CSS text (`content: "Response"`): words the card does not need (a pill over a figure,
  a word beside an icon button); the button keeps its accessible name. Every visible word that carries data stays the
  standard view's. A status keeps its icon and word (`attr(title)`, `attr(data-word)` where the standard view puts
  the word there).
- Not hide anything the standard view shows, nor take an element out of the keyboard order.
- What CSS cannot do is a part of the theme's own: a chart style (theme.json `"chart"`, e.g. the bar sparklines) or,
  for a different structure, a [view of the card](#drawing-a-card) that composes the standard parts.

## Converting a card to a model and views

For the cards still drawn the old way (each panel module fetching and building its DOM in the same functions). The
three converted cards are the references: **services** (a keyed list, the simplest), **diagnosis** (several parts,
live data every second, notices with actions, the header pill as page chrome) and **latency** (canvas charts, a
legend, a mode switch, the history views feeding the same model). Steps:

1. **Read the card and its UI steps** (`tests/ui/steps/<id>.mjs` and any other step that touches it): they must pass
   unchanged, so the standard view's DOM stays exactly what it is now (classes, ids, `data-*` the steps read, text).
2. **Split the files**: `model.js` (fetching, timers, state, actions and their `registerActions`, `present(...)` ->
   view model, `fixture()`), `view.js` (the standard view, `export default {parts, mount}`), `panel.js` stays the
   registry entry and calls the model's mount. Keep exported names other modules or steps import (latency kept
   `charts.js` and its `view` data object because a step imports it).
3. **Write `present()`**: everything the old render functions computed for the text (formatting, words, which status,
   which actions with which arguments) moves into it; it reads only its arguments (pass `now`, the target list, the open
   rows), so `fixture()` can call it with fixed data. Replace a part of the view model only when it changed.
4. **Write the standard view** from the old render code: the same markup, built inside `root` (no `getElementById` on the
   body; ids stay in `panel.html` for the steps and for other modules), patched in place (`kit.text`, `kit.html`,
   `kit.list`, or the card's own comparisons), with its observers undone in `unmount`.
5. **Mark it**: `data-v` on every value, `data-st` on every status (icon and word), `data-item` on rows; actions and help
   are marked already. Add attributes and wrap bare text in a `<span>` only where it changes nothing on screen.
6. **Keep the frame in the model**: what the header and footer show (a summary, the card's `data-status`) and anything
   outside the card (the header's status pill, the page title) is written by the model.
7. **Create the card** in the model's mount: `createCard({panel, root: article.querySelector('.panel-b'), standard,
   fixture})`; for a panel with one card per device, one per instance (the router and line cards: also one part per
   capability in `parts`, and the view model's parts keyed by capability).
8. **Remove what the engine does**: a `themechange` listener that redrew the card (the engine calls `update` again),
   module-level DOM references that a theme switch would make stale.
9. **Check**: `scripts/test.sh -p no:warnings tests/test_frontend.py tests/test_themes.py`, `scripts/test-ui.sh` (the
   card's steps unchanged and green; the parity step now covers the card), and the review screenshots of the default
   theme before and after, which must look the same.

## Checks every theme must pass

`tests/test_themes.py` runs every folder through these, in each of its modes, with no new test to write
(`pengumon/web/theme_checks.py` has the arithmetic):

1. **The contract:** `theme.json` as above; every color token present; values of the right kind; a skin's selectors
   scoped; an effect with a default export `{start}` and no `requestAnimationFrame` or `setInterval`; the card views
   in `panels/` as in [Drawing a card](#how-a-theme-declares-views); a chart style in range.
2. **Text contrast (WCAG 2 AA):** `--ink-1` and `--ink-2` 4.5:1 on `--bg`, `--surface-1`, `--surface-2` and
   `--surface-3`; `--ink-3` 4.5:1 on `--bg`, `--surface-1` and `--surface-2`; `--ink-4` (small non-essential text) 3:1 on
   the cards; `--accent` 4.5:1 on the cards (it is the link color); `--on-accent` 4.5:1 on `--accent`; `--focus` 3:1 on
   the backgrounds; every status color 3:1 on `--surface-1`; good, warn, crit and busy 4.5:1 on their own `-bg` tint
   (a status chip's word). A translucent color is laid over the surface it sits on first.
3. **Status colors apart:** good, warn, crit and busy at least 12 apart from one another (OKLab distance x 100).
4. **Series colors apart:** each pair of neighbors `--s1`/`--s2` ... `--s7`/`--s8` at least 8 apart (OKLab x 100), and
   at least 6 apart as people with deuteranopia and protanopia see them (simulated with Machado, Oliveira and
   Fernandes 2009, full severity).

What the test cannot check, the reviewer does, at 1440 and 390 px in each mode the theme has: every panel and dialog
readable, keyboard focus visible, no sideways scrolling on the phone, the effect subtle: open the page with the theme
picked (this browser's theme menu, or `localStorage` `pengumon.look` in a scripted browser) and look.

To check a palette while making it, run the test for your folder:
`scripts/test.sh -p no:warnings tests/test_themes.py -k <id>`; a failure names the token pair and the number.

## How the page applies a theme

- The server writes the default theme on `<html data-look="<id>">` (and `data-theme` when that theme has one mode), the
  list of themes on `<html data-looks>` and the effects setting on `data-effects`. It links no theme's styles itself:
  `tokens.css` (the default theme) and the custom themes' token blocks (`<style id="customThemeCss">`) are on every page.
- `static/js/theme-boot.js`, a classic script in `<head>` right after the page's own stylesheets, puts this browser's
  choice there before anything is drawn: `localStorage` `pengumon.look` (a theme id; none: the default) and
  `pengumon.theme` (`light` or `dark`; none: the system's). It then writes the link to that theme's stylesheet (`css` in
  its entry of `data-looks`: `/static/v/<tree>/themes/<id>/theme.css`, see [Skins](#skins)) where it stands, with
  `document.write`: a stylesheet the parser makes holds the first paint until it has loaded, as the page's own do, and
  it comes after them in the cascade as the skins always have. So the page never flashes the wrong theme, and only the
  theme in use is loaded (none for the default theme and the custom ones).
- The generated stylesheet has, per theme, a block on `:root[data-look="<id>"]`, and for a theme with both modes its
  light block where the system asks for light (unless the person picked dark) and where the person picked light; then
  the skin and the card views' styles.
- `static/js/themes.js` then reads the tokens and the chart style for the canvas charts (`chart-theme.js`), starts the
  effect, paints the cards' own colors and sends a `themechange` event, on which `static/js/cards.js` shows each card's
  view for the theme and modules that draw on canvas redraw.
- **A switch** (the header menu, another tab, Settings): `themes.js` links the new theme's stylesheet next to the old one
  (`link[data-look-css="<id>"]`) and imports its card views, and changes `data-look` once both have loaded (or failed:
  the theme then shows with the standard styles, a card with its standard view), so there is no moment in the new theme
  without its styles, nor a card still in the old theme's view without them (the cards swap views before the next
  paint); then it unlinks the old one. A switch to a theme whose stylesheet and views are there, or that has none (the
  default, a custom theme, light or dark), is at once. After a Save in Settings the page is themed again only when the
  theme it shows, the effects switch or a custom theme's colors changed; a layout change that keeps the cards' own
  colors redraws nothing.
- `<html data-looks>` also carries, for a theme that has them, its card views (`views`: panel id to module URL) and its
  chart style (`chart`); `theme-boot.js` preloads the views of the theme the page starts with.
- This browser's effects choice is `localStorage` `pengumon.effects` (`on` or `off`; none: the Settings one).

## Custom themes

Settings > Display > Themes lists the built-in themes, each with **Export** (the theme as a `theme.json`) and
**Duplicate**, and the owner's own themes. Own themes are stored in the settings (`display.themes`, a list like the
webhooks), so every browser has them, and are made by:

- **Duplicate**: a copy of any theme's colors, then edited color by color (a color picker for each, or the text of a
  translucent one);
- **Import a theme file**: any `theme.json`; colors it lacks come from the standard theme, and what an own theme cannot
  have (fonts, shapes, effects) is left out, with a note that says so.

An own theme has the id `custom-<name>`, colors (and any text sizes and corner radii a file brings, within the ranges)
and nothing else: no skin, no effect, no card views, no chart style. The server checks every one on Save (`ThemeItem` in `pengumon/web/settings.py`:
known token names, color values, sizes in range, no CSS text) before its values are written into the page. The
contrast checks above are not enforced on own themes; that is the owner's call. `theme_warnings` in
`pengumon/web/settings.py` lists what they would find in own themes, as warnings and never errors; two own themes cannot
share an id.

## Card colors

The Layout dialog gives each card **Colors: Page theme / <any theme> / Custom**. A theme's colors for a card are its
accent (with its tint, the text on it and the focus ring) and its eight series colors, in the page's current mode (or the
theme's only mode); Custom is the accent and the eight series colors, picked one by one. They are custom properties on
that card's `<section data-panel>`, so the card's CSS and its canvas chart (through `token(name, element)` in
`chart-theme.js`) follow; the rest of the card (backgrounds, text, status) keeps the page theme, and a target keeps its
own color everywhere. So that every card takes every theme's colors (and its own), card and dialog styles, skins,
card views, card markup and card code write no color literal: `tests/test_frontend.py`
(`test_cards_and_dialogs_take_their_colors_from_the_tokens`) fails on one, the made-up data of a card's fixture, the
fallbacks `js/targets.js` and `js/layout.js` document and the targets' configured colors aside. The choice is kept in this
browser's layout (`pengumon.layout.v1`); Settings > Display > "Use this browser's layout" makes it the one new browsers
start with (`dashboard.colors`).
