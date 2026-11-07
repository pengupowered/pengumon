// Incidents card, the model: the list the panel works from (list.js), search, paging, the busy-line toggle, the time window
// another panel picked, and the view model every view of the card draws (view.js is the standard one; docs/themes.md,
// "Drawing a card"). The body is the 7-day timeline, the window bar and the list of incidents (one line each: what, status,
// when, how long, where; the description, load and evidence are the inspector's, which a row opens: the inspector is a
// dialog, not a card). The header (count, busy-line toggle, filter, page size) and the footer (pager) are the frame, kept
// here.

import { registerActions } from '../../js/dispatch.js';
import { createCard } from '../../js/cards.js';
import { icon, localFormat, nextMidnight, STATUS } from '../../js/util.js';
import { CAUSES } from '../../js/path-api.js';
import { CAP, hasMore, isLoading, loadAll, loadMore, loadSince, oldestLoaded, totalCounts } from './list.js';
import { causeOf, durationOf, levelOfIncident, whenText } from './incidents.js';
import standard from './view.js';

const DAY = 86400;

let card = null;
let root = null;
let allRawIncidents = [];
let incidentSearchQuery = '';
let incidentPageSize = 10;
let incidentCurrentPage = 1;
let showExpectedIncidents = false;
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

// The view model from the card's state `s` ({all, query, pageSize, page, showExpected, window, totals, more, loading,
// oldest}) and the moment: { timeline, window, list: {rows, empty}, frame, page, total } (page: the current page, kept inside the
// pages there are). `window` is the bar naming the time another panel picked (its element has data-role="window": the
// model focuses it when the panel is opened from another one); `frame` is what the header and footer show.
export function present(s, nowMs) {
  const visible = s.showExpected ? s.all : s.all.filter((inc) => !inc.expected);
  let filtered = s.window ? visible.filter((inc) => overlaps(inc, s.window)) : visible;
  if (s.query) {
    filtered = filtered.filter((inc) => (inc.title && inc.title.toLowerCase().includes(s.query))
      || (inc.description && inc.description.toLowerCase().includes(s.query))
      || (inc.severity && inc.severity.toLowerCase().includes(s.query))
      || (inc.cause_code && inc.cause_code.toLowerCase().includes(s.query)));
  }
  const totalCount = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / s.pageSize));
  const page = Math.min(s.page, totalPages);

  // the server's count when it gave one, else what is loaded so far ("+" while there may be more)
  const known = s.totals;
  // the total shown is that same count wherever a total appears, while more is still to load (unfiltered; a filter counts what matches)
  const unfiltered = !s.query && !s.window;
  const knownShown = known ? (s.showExpected ? known.total : known.total - known.expected) : null;
  const shownTotal = knownShown !== null && unfiltered && s.more ? Math.max(totalCount, knownShown) : totalCount;
  const expectedCount = known ? known.expected : s.all.filter((inc) => inc.expected).length;

  const startIdx = (page - 1) * s.pageSize;
  const endIdx = Math.min(startIdx + s.pageSize, totalCount);
  const items = filtered.slice(startIdx, endIdx);
  let empty = null;
  if (!items.length) {
    const [head, text] = s.query ? ['Nothing to show', 'No incident matches the filter.']
      : s.window ? emptyWindowText(s, s.window) : ['Nothing to show', 'No network incidents recorded.'];
    empty = { head, text, icon: 'ok' };
  }
  return {
    timeline: timeline(visible, s.window, nowMs),
    window: s.window ? { label: s.window.label, found: totalCount === 1 ? '1 incident' : `${totalCount} incidents`,
      clear: { action: 'clearIncidentWindow', label: 'Show all', icon: 'close' } } : null,
    list: { rows: items.map(row), empty },
    page,
    total: totalCount,
    frame: {
      count: known ? `${s.showExpected ? known.total : known.total - known.expected} recorded events` : `${visible.length}${s.more ? '+' : ''} recorded events`,
      expected: { hidden: expectedCount === 0, text: `${s.showExpected ? 'Hide' : 'Show'} ${expectedCount} expected`, pressed: s.showExpected },
      info: totalCount === 0 ? 'Showing 0 events'
        : `Showing ${startIdx + 1}–${endIdx} of ${shownTotal}${s.more && unfiltered && knownShown === null ? '+' : ''} events${s.query || s.window ? ' (filtered)' : ''}`,
      pageNum: `Page ${page} of ${totalPages}`,
      first: page <= 1, prev: page <= 1,
      next: page >= totalPages && !s.more, last: page >= totalPages && !s.more
    }
  };
}

// Made-up incidents for the parity test of the card's views: an ongoing outage, a degraded one with a loss span, an expected
// busy-line event and a window naming an hour.
export function fixture() {
  const now = Date.now() / 1000;
  const inc = (id, extra) => ({ id, title: `Example incident ${id}`, severity: 'warning', cause: 'gateway', expected: false, description: '', start_time: now - 3600 * id, end_time: now - 3600 * id + 120,
    duration_seconds: 120, loss_seconds: 90, ...extra });
  const all = [
    inc(1, { title: 'Example outage', severity: 'critical', end_time: null, duration_seconds: null, loss_seconds: null }),
    inc(2, { cause: 'line' }),
    inc(3, { title: 'Example busy line', cause: 'load', expected: true, duration_seconds: 600, end_time: now - 3600 * 3 + 600, loss_seconds: 0 })
  ];
  const win = { start: now - 4 * 3600, end: now, label: 'Example hour', lost: 3, sent: 60 };
  return present({ all, query: '', pageSize: 10, page: 1, showExpected: true, window: win, totals: null, more: false, loading: false, oldest: null }, now * 1000);
}

// ---------- the card ----------

function state() {
  return { all: allRawIncidents, query: incidentSearchQuery, pageSize: incidentPageSize, page: incidentCurrentPage, showExpected: showExpectedIncidents,
    window: incidentWindow, totals: totalCounts(), more: hasMore(), loading: isLoading(), oldest: oldestLoaded() };
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
    location.hash = tab;
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
  toggleExpectedIncidents: () => toggleExpectedIncidents()
});
registerActions('input', {
  handleIncidentSearch: (el) => handleIncidentSearch(el.value)
});
registerActions('change', {
  changeIncidentPageSize: (el) => changeIncidentPageSize(el.value)
});
