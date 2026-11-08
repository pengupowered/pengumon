// Incidents card, the model: the list the panel works from (list.js), search, paging, the busy-line toggle, the time window
// another panel picked, and the view model every view of the card draws (view.js is the standard one; docs/themes.md,
// "Drawing a card"). The body is the 7-day timeline, the window bar and the list of incidents (one line each: what, status,
// when, how long, where; the description, load and evidence are the inspector's, which a row opens: the inspector is a
// dialog, not a card). Drops that belong together (an outage of 2 or more, GET /api/outages) are one row that opens to its
// drops, which are the incident rows as they always were; an outage is shown whole (all its drops loaded) or not yet. The header (count, busy-line toggle, filter, page size) and the footer (pager) are the frame, kept
// here.

import { registerActions } from '../../js/dispatch.js';
import { createCard } from '../../js/cards.js';
import { afterModalHistory, icon, localFormat, nextMidnight, STATUS } from '../../js/util.js';
import { CAUSES, CAUSE_SHORT } from '../../js/path-api.js';
import { CAP, hasMore, isLoading, loadAll, loadMore, loadSince, loadedOutages, oldestLoaded, totalCounts } from './list.js';
import { causeOf, durationOf, formatDuration, levelOfIncident, whenText } from './incidents.js';
import { settingsValues, val } from '../../js/settings-values.js';
import standard from './view.js';

const DAY = 86400;

let card = null;
let root = null;
let allRawIncidents = [];
let incidentSearchQuery = '';
let incidentPageSize = 10;
let incidentCurrentPage = 1;
let showExpectedIncidents = false;
const openOutages = new Set(); // outage ids whose drops are listed (state that survives a theme switch)
let incidentWindow = null; // {start, end, label, lost, sent}: only the incidents overlapping this time (showIncidentWindow)

const overlaps = (inc, win) => inc.start_time < win.end && (inc.end_time ?? Date.now() / 1000) > win.start;

// ---------- the view model ----------

// The last 7 days as one lane per cause, each incident a bar where and as long as it happened (busy-line ones hatched,
// an ongoing one ringed); a bar opens the inspector. `visible`: the incidents the toggle shows. Positions are percents of
// the lane (bars) or fractions of the week (now, the window).
function timeline(visible, win, nowMs) {
  const now = nowMs / 1000;
  const end = nextMidnight(nowMs) / 1000; // the end of today, on the page's clock
  const start = end - 7 * DAY;
  const list = visible.filter((i) => (i.end_time ?? now) >= start);
  if (!list.length) return null;
  const span = end - start;
  const shownWin = win && win.end > start ? win : null;
  return {
    lanes: CAUSES.filter(([key]) => list.some((i) => (i.cause || 'unknown') === key)).map(([key, word]) => ({
      key, word,
      bars: list.filter((i) => (i.cause || 'unknown') === key).map((i) => {
        const active = i.duration_seconds === null || i.duration_seconds === undefined;
        const label = `${i.title} · ${whenText(i.start_time)} · ${active ? 'ongoing' : durationOf(i).long}${i.expected ? ' · expected' : ''}`;
        return { id: i.id, level: levelOfIncident(i), expected: !!i.expected, ongoing: active,
          left: ((Math.max(start, i.start_time) - start) / span) * 100, width: Math.max(0.45, (((i.end_time ?? now) - i.start_time) / span) * 100),
          tip: label, aria: label.replace(/ · /g, ', ') };
      })
    })),
    days: Array.from({ length: 7 }, (_, k) => localFormat((start + k * DAY + 3600) * 1000, { weekday: 'short' })),
    now: (now - start) / span,
    window: shownWin ? { from: (Math.max(start, shownWin.start) - start) / span, to: (shownWin.end - start) / span } : null
  };
}

// Why a window has no incident to list: busy-line ones hidden by the toggle, older than the loaded list, nothing probed
// then, nothing lost, or loss too small or brief to open one. `lost` / `sent` come from an hour of When it breaks,
// `state` from an Uptime block (that one target's block); either may be missing.
function emptyWindowText(s, win) {
  const hidden = s.showExpected ? 0 : s.all.filter((inc) => inc.expected && overlaps(inc, win)).length;
  if (hidden) return ['Only busy-line events here', `${hidden === 1 ? 'One incident' : `${hidden} incidents`} in this time ${hidden === 1 ? 'was' : 'were'} explained by a busy line. Show expected to list ${hidden === 1 ? 'it' : 'them'}.`];
  if (s.loading && s.more && win.start < (s.oldest ?? Infinity)) return ['Loading', 'Looking for older incidents.'];
  const oldest = !s.more && s.all.length >= CAP ? s.oldest : null;
  if (oldest !== null && win.start < oldest) return ['Older than the list', `Only the newest ${CAP} incidents are loaded, and this time is before them.`];
  if (win.sent === 0 || win.state === 'no_data') return ['No probes in this time', 'Nothing was probed then (Pengumon was not running), so there is nothing to list.'];
  if (win.sent > 0 && win.lost === 0) return ['Nothing failed in this time', `All ${win.sent.toLocaleString()} probes answered and no incident was open.`];
  if (win.state === 'ok') return ['Nothing failed in this time', 'No incident was open, and this target lost no probes.'];
  const lost = win.lost ? `${win.lost.toLocaleString()} of ${win.sent.toLocaleString()} probes were lost, ` : 'The probes lost here were ';
  return ['No incident in this time', `${lost}too few or too brief to count as an incident (short drops are still in the loss figures).`];
}

// One incident as the views draw it.
function row(inc) {
  const level = levelOfIncident(inc);
  const active = inc.duration_seconds === null || inc.duration_seconds === undefined;
  const dur = durationOf(inc);
  return {
    id: inc.id,
    expected: !!inc.expected,
    title: inc.title,
    status: active ? { level, word: 'Ongoing', icon: STATUS[level].icon, quiet: false }
      : { level, word: inc.expected ? 'Expected' : level === 'crit' ? 'Outage' : 'Degraded', icon: STATUS[level].icon, quiet: true },
    when: { text: whenText(inc.start_time), title: localFormat(inc.start_time * 1000, { dateStyle: 'short', timeStyle: 'medium' }) },
    duration: { text: dur.text, title: dur.title, lost: dur.lost },
    cause: causeOf(inc),
    open: { action: 'inspectIncident', arg: inc.id, label: `${inc.title}: open the details` }
  };
}

// A span as the summary line says it: "40 s", "14 min", "1h 05m".
const spanText = (seconds) => (seconds < 60 ? `${Math.round(seconds)} s` : seconds < 3600 ? `${Math.round(seconds / 60)} min` : formatDuration(seconds));

// One outage of 2 or more drops as the views draw it: the incident row's fields, a summary line, the label when it is set
// and the control that lists its drops (the rows after it, `drops`, are in the view model only while it is open).
function outageRow(o, members, complete, maintenance) {
  const level = o.severity === 'critical' ? 'crit' : 'warn';
  const open = openOutages.has(o.id);
  const down = o.down_seconds > 0 ? ` · down ${spanText(o.down_seconds)}` : '';
  const loaded = complete ? '' : ` (${members.length} loaded)`;
  const stateNote = o.ongoing ? 'Ongoing' : o.settling ? 'may continue' : null;
  const span = formatDuration(o.duration_seconds);
  return {
    id: `o${o.id}`,
    kind: 'outage',
    expected: false,
    title: o.title,
    status: { level, word: level === 'crit' ? 'Outage' : 'Degraded', icon: STATUS[level].icon, quiet: !o.ongoing },
    when: { text: whenText(o.start_time), title: localFormat(o.start_time * 1000, { dateStyle: 'short', timeStyle: 'medium' }) },
    duration: { text: o.ongoing ? `${span} so far` : span, lost: false,
      title: `From the first drop's start to the last drop's end, ${span}, with the time between the drops. Down: the time a drop was critical.` },
    cause: o.places.map((p) => CAUSE_SHORT[p] || p).join(' · '),
    summary: `${o.drops} drops${down}${loaded}`,
    state: stateNote,
    maintenance: o.likely_maintenance ? { text: 'likely maintenance',
      title: `Started between ${maintenance.start} and ${maintenance.end} at the provider: often planned work. Only a label: the downtime counts in full.` } : null,
    toggle: { action: 'toggleOutage', arg: o.id, expanded: open,
      label: `${o.title}: ${open ? 'hide' : 'show'} its ${o.drops} drops` },
    open: { action: 'toggleOutage', arg: o.id, label: `${o.title}: ${open ? 'hide' : 'show'} its ${o.drops} drops` },
    drops: open ? members.map((m) => ({ ...row(m), kind: 'drop' })) : []
  };
}

// The rows of the list from the incidents loaded and the outages read (`s.outages`): an incident is a row, the drops of an
// outage of 2 or more are one row (placed at its start), newest first. `matches`: whether an incident passes the filter and
// the time window; an outage passes when any drop does (and then lists all of them). An outage with drops not loaded yet is
// held back while there is more to load (list.js loads them), and shown with what is loaded at the end of the list.
function buildRows(s, visible, matches, maintenance) {
  const groups = new Map();
  (s.outages || []).filter((o) => o.drops >= 2).forEach((o) => o.incident_ids.forEach((id) => groups.set(id, o)));
  const byId = new Map(s.all.map((inc) => [inc.id, inc]));
  const seen = new Set();
  const found = [];
  visible.forEach((inc) => {
    const o = groups.get(inc.id);
    if (!o) {
      if (matches(inc)) found.push({ at: inc.start_time, row: () => row(inc) });
      return;
    }
    if (seen.has(o.id)) return;
    seen.add(o.id);
    const members = o.incident_ids.map((id) => byId.get(id)).filter(Boolean);
    const complete = members.length === o.incident_ids.length;
    if (!complete && s.more) return;
    if (members.some(matches)) found.push({ at: o.start_time, row: () => outageRow(o, members, complete, maintenance) });
  });
  return found.sort((a, b) => b.at - a.at);
}

// The view model from the card's state `s` ({all, query, pageSize, page, showExpected, window, totals, more, loading,
// oldest}) and the moment: { timeline, window, list: {rows, empty}, frame, page, total } (page: the current page, kept inside the
// pages there are). `window` is the bar naming the time another panel picked (its element has data-role="window": the
// model focuses it when the panel is opened from another one); `frame` is what the header and footer show.
export function present(s, nowMs) {
  const visible = s.showExpected ? s.all : s.all.filter((inc) => !inc.expected);
  const maintenance = s.maintenance || { start: '00:00', end: '06:00' };
  const matches = (inc) => {
    if (s.window && !overlaps(inc, s.window)) return false;
    return !s.query || (inc.title && inc.title.toLowerCase().includes(s.query))
      || (inc.description && inc.description.toLowerCase().includes(s.query))
      || (inc.severity && inc.severity.toLowerCase().includes(s.query))
      || (inc.cause_code && inc.cause_code.toLowerCase().includes(s.query));
  };
  const everything = buildRows(s, visible, () => true, maintenance);
  const filtered = s.query || s.window ? buildRows(s, visible, matches, maintenance) : everything;
  const totalCount = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / s.pageSize));
  const page = Math.min(s.page, totalPages);

  // the incidents behind the rows: the server's count when it gave one, else what is loaded so far ("+" while there may be more)
  const known = s.totals;
  const unfiltered = !s.query && !s.window;
  const knownShown = known ? (s.showExpected ? known.total : known.total - known.expected) : null;
  const incidentCount = knownShown !== null ? knownShown : visible.length;
  const grouped = everything.length !== visible.length; // (rows are fewer than incidents once an outage has 2 or more drops)
  // the total shown, where a total appears, while more is still to load (unfiltered): rows are known only as far as loaded
  const shownTotal = !grouped && knownShown !== null && unfiltered && s.more ? Math.max(totalCount, knownShown) : totalCount;
  const plus = s.more && unfiltered && (grouped || knownShown === null) ? '+' : '';
  // the list holds the newest CAP incidents; when the history has more, say so (the count above is the server's real one)
  const capped = !s.more && s.all.length >= CAP && (!known || known.total > s.all.length);
  const capNote = !capped ? '' : `Only the newest ${CAP} incidents are listed${known ? ` (of ${known.total.toLocaleString()} recorded)` : ''}. Older ones are in the ISP dossier (up to 30 days), the reliability report (up to 90 days) and the API (/api/incidents with limit and offset).`;
  const expectedCount = known ? known.expected : s.all.filter((inc) => inc.expected).length;

  const startIdx = (page - 1) * s.pageSize;
  const endIdx = Math.min(startIdx + s.pageSize, totalCount);
  const items = filtered.slice(startIdx, endIdx).map((f) => f.row());
  // an open outage's drops follow its row (they are not rows of the pager)
  const rows = items.flatMap((r) => [r, ...(r.drops || [])]);
  let empty = null;
  if (!items.length) {
    const [head, text] = s.query ? ['Nothing to show', 'No incident matches the filter.']
      : s.window ? emptyWindowText(s, s.window) : ['Nothing to show', 'No network incidents recorded.'];
    empty = { head, text, icon: 'ok' };
  }
  const windowIncidents = s.window ? visible.filter(matches).length : 0;
  return {
    timeline: timeline(visible, s.window, nowMs),
    window: s.window ? { label: s.window.label, found: windowIncidents === 1 ? '1 incident' : `${windowIncidents} incidents`,
      clear: { action: 'clearIncidentWindow', label: 'Show all', icon: 'close' } } : null,
    list: { rows, empty },
    page,
    total: totalCount,
    frame: {
      cap: capNote,
      count: capped ? `${incidentCount.toLocaleString()} recorded events · newest ${CAP} listed`
        : grouped ? `${everything.length}${s.more ? '+' : ''} events · ${incidentCount} incidents`
        : `${incidentCount}${s.more && knownShown === null ? '+' : ''} recorded events`,
      expected: { hidden: expectedCount === 0, text: `${s.showExpected ? 'Hide' : 'Show'} ${expectedCount} expected`, pressed: s.showExpected },
      info: totalCount === 0 ? 'Showing 0 events'
        : `Showing ${startIdx + 1}–${endIdx} of ${shownTotal}${plus} events${s.query || s.window ? ' (filtered)' : ''}${capped ? `, from the newest ${CAP} incidents` : ''}`,
      pageNum: `Page ${page} of ${totalPages}`,
      first: page <= 1, prev: page <= 1,
      next: page >= totalPages && !s.more, last: page >= totalPages && !s.more
    }
  };
}

// Made-up incidents for the parity test of the card's views: an ongoing incident, a degraded one with a loss span, an expected
// busy-line event, an outage of 3 drops (open, with the maintenance label) and a window naming an hour.
export function fixture() {
  const now = Date.now() / 1000;
  const inc = (id, extra) => ({ id, title: `Example incident ${id}`, severity: 'warning', cause: 'gateway', expected: false, description: '', start_time: now - 3600 * id, end_time: now - 3600 * id + 120,
    duration_seconds: 120, loss_seconds: 90, ...extra });
  const all = [
    inc(1, { title: 'Example outage', severity: 'critical', end_time: null, duration_seconds: null, loss_seconds: null }),
    inc(2, { cause: 'line' }),
    inc(3, { title: 'Example busy line', cause: 'load', expected: true, duration_seconds: 600, end_time: now - 3600 * 3 + 600, loss_seconds: 0 }),
    // three drops of one provider event, the first inside the maintenance hours
    inc(11, { title: 'Example line drop', severity: 'critical', cause: 'line', start_time: now - 3600 * 2, end_time: now - 3600 * 2 + 600, duration_seconds: 600 }),
    inc(12, { title: 'Example line drop', severity: 'critical', cause: 'line', start_time: now - 3600 * 2 + 900, end_time: now - 3600 * 2 + 1200, duration_seconds: 300 }),
    inc(13, { title: 'Example ISP loss', cause: 'isp', start_time: now - 3600 * 2 + 1500, end_time: now - 3600 * 2 + 1560, duration_seconds: 60 })
  ];
  const outages = [{ id: 11, start_time: now - 3600 * 2, end_time: now - 3600 * 2 + 1560, ongoing: false, settling: false, duration_seconds: 1560,
    down_seconds: 900, affected_seconds: 1260, drops: 3, critical_drops: 2, severity: 'critical', places: ['line', 'isp'], title: 'Example line drop',
    cause_code: 'access_line_drop', likely_maintenance: true, incident_ids: [11, 12, 13] }];
  openOutages.add(11);
  const win = { start: now - 4 * 3600, end: now, label: 'Example hour', lost: 3, sent: 60 };
  const vm = present({ all, query: '', pageSize: 10, page: 1, showExpected: true, window: win, totals: null, more: false, loading: false, oldest: null,
    outages, maintenance: { start: '00:00', end: '06:00' } }, now * 1000);
  openOutages.delete(11);
  return vm;
}

// ---------- the card ----------

function state() {
  return { all: allRawIncidents, query: incidentSearchQuery, pageSize: incidentPageSize, page: incidentCurrentPage, showExpected: showExpectedIncidents,
    window: incidentWindow, totals: totalCounts(), more: hasMore(), loading: isLoading(), oldest: oldestLoaded(),
    outages: loadedOutages(), maintenance: { start: val(settingsValues(), 'incidents.maintenance_start', '00:00'), end: val(settingsValues(), 'incidents.maintenance_end', '06:00') } };
}

// Draws the card and its frame from the state now.
function render() {
  if (!card) return;
  const vm = present(state(), Date.now());
  incidentCurrentPage = vm.page;
  card.update(vm);
  const f = vm.frame;
  const set = (id, fn) => { const el = document.getElementById(id); if (el) fn(el); };
  set('incidentCountBadge', (el) => { el.textContent = f.count; });
  set('btnToggleExpected', (el) => {
    el.hidden = f.expected.hidden;
    el.innerHTML = `${icon('busy')}${f.expected.text}`;
    el.setAttribute('aria-pressed', String(f.expected.pressed));
    el.title = 'Incidents explained by a busy line (expected, not a fault)';
  });
  set('incidentCapNote', (el) => { el.hidden = !f.cap; el.textContent = f.cap; });
  set('incidentPaginationInfo', (el) => { el.textContent = f.info; });
  set('incidentPageNum', (el) => { el.textContent = f.pageNum; });
  set('btnPageFirst', (el) => { el.disabled = f.first; });
  set('btnPagePrev', (el) => { el.disabled = f.prev; });
  set('btnPageNext', (el) => { el.disabled = f.next; });
  set('btnPageLast', (el) => { el.disabled = f.last; });
}

export function initIncidentsCard(section) {
  root = section.querySelector('.panel-b');
  card = createCard({ panel: 'incidents', root, standard, fixture });
  document.addEventListener('zonechange', render); // the times and the timeline's days are on the page's clock
}

// Lists the drops of an outage under its row, or hides them again.
export function toggleOutage(id) {
  if (openOutages.has(id)) openOutages.delete(id);
  else openOutages.add(id);
  render();
}

export function toggleExpectedIncidents() {
  showExpectedIncidents = !showExpectedIncidents;
  incidentCurrentPage = 1;
  render();
}

export function updateIncidentsTable(incidents) {
  if (incidents) {
    allRawIncidents = incidents;
  }
  render();
}

// The incidents overlapping a stretch of time, from a cell of another panel (When it breaks, Uptime): the list shows only
// those, the timeline marks the stretch, and the panel comes into view (on a phone its tab is opened first).
export function showIncidentWindow(win) {
  incidentWindow = win;
  incidentCurrentPage = 1;
  render();
  if (!win) return;
  loadSince(win.start); // older than what is loaded: load back to it (list.js re-renders)
  const section = document.querySelector('[data-panel=incidents]');
  if (!section) return;
  const reveal = () => requestAnimationFrame(() => {
    const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    // the section is display: contents (style.css), so the card inside it is what has a place on the page
    (section.querySelector('.panel') || section).scrollIntoView({ block: 'start', behavior: smooth ? 'smooth' : 'auto' });
    root.querySelector('[data-role="window"]')?.focus({ preventScroll: true });
  });
  const tab = (section.dataset.tab || '').split(/\s+/)[0];
  // on a phone the panel's tab opens first; tabs.js shows it on hashchange, so the scroll waits for that
  if (section.hidden && tab && location.hash !== `#${tab}`) {
    window.addEventListener('hashchange', reveal, { once: true });
    afterModalHistory(() => { location.hash = tab; }); // the dialog that sent us here (Show in latency chart) gives its history entry back first
  } else reveal();
}

export function handleIncidentSearch(query) {
  incidentSearchQuery = (query || '').trim().toLowerCase();
  incidentCurrentPage = 1;
  render();
  if (incidentSearchQuery && hasMore()) loadAll(); // a search looks through everything the list can hold (list.js re-renders)
}

export function changeIncidentPageSize(val) {
  incidentPageSize = parseInt(val, 10) || 10;
  incidentCurrentPage = 1;
  render();
}

// How many incidents the filter, the window and the toggle leave (all pages).
const filteredCount = () => present(state(), Date.now()).total;

export function gotoIncidentPage(action, loaded = false) {
  const filtered = filteredCount();
  const totalPages = Math.max(1, Math.ceil(filtered / incidentPageSize));
  // past the last loaded page: load the older ones first (the last page is the oldest there is)
  if (!loaded && hasMore() && (action === 'last' || (action === 'next' && incidentCurrentPage >= totalPages))) {
    // a last page that was not full is filled first (the new rows continue it); stepping on would skip them
    const partial = action === 'next' && filtered < totalPages * incidentPageSize;
    (action === 'last' ? loadAll() : loadMore()).then(() => (partial ? render() : gotoIncidentPage(action, true)));
    return;
  }

  if (action === 'first') incidentCurrentPage = 1;
  else if (action === 'prev') incidentCurrentPage = Math.max(1, incidentCurrentPage - 1);
  else if (action === 'next') incidentCurrentPage = Math.min(totalPages, incidentCurrentPage + 1);
  else if (action === 'last') incidentCurrentPage = totalPages;
  else if (typeof action === 'number') incidentCurrentPage = Math.min(totalPages, Math.max(1, action));

  render();
}

// A probe count from a cell's data-* attribute, left out when the cell does not carry one.
const count = (el, key) => (el.dataset[key] === undefined ? {} : { [key]: Number(el.dataset[key]) });

// The actions this card's markup names (see dispatch.js).
registerActions('click', {
  gotoIncidentPage: (el) => gotoIncidentPage(el.dataset.arg),
  // markup of other panels names this one: data-start / data-end (epoch seconds), data-label, data-lost / data-sent, data-state
  showIncidentWindow: (el) => showIncidentWindow({ start: Number(el.dataset.start), end: Number(el.dataset.end),
    label: el.dataset.label, state: el.dataset.state, ...count(el, 'lost'), ...count(el, 'sent') }),
  clearIncidentWindow: () => showIncidentWindow(null),
  toggleExpectedIncidents: () => toggleExpectedIncidents(),
  toggleOutage: (el) => toggleOutage(Number(el.dataset.arg))
});
registerActions('input', {
  handleIncidentSearch: (el) => handleIncidentSearch(el.value)
});
registerActions('change', {
  changeIncidentPageSize: (el) => changeIncidentPageSize(el.value)
});
