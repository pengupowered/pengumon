// Incident list: search, paging, the busy-line toggle, the time window and the table rows. A row is one line (what,
// status, when, how long, where); the description, load and evidence are the inspector's, which a row opens.

import { registerActions } from '../../js/dispatch.js';
import { badge, escapeHtml, icon } from '../../js/util.js';
import { CAUSES, CAUSE_SHORT } from '../../js/path-api.js';
import { CAP, hasMore, isLoading, loadAll, loadMore, loadSince, oldestLoaded, totalCounts } from './list.js';

let allRawIncidents = [];
let incidentSearchQuery = '';
let incidentPageSize = 10;
let incidentCurrentPage = 1;
let showExpectedIncidents = false;
let incidentWindow = null; // {start, end, label, lost, sent}: only the incidents overlapping this time (showIncidentWindow)

export function toggleExpectedIncidents() {
  showExpectedIncidents = !showExpectedIncidents;
  incidentCurrentPage = 1;
  renderIncidentsPage();
  renderTimeline();
}

export function updateIncidentsTable(incidents) {
  if (incidents) {
    allRawIncidents = incidents;
  }
  renderIncidentsPage();
  renderTimeline();
}

// The last 7 days as one lane per cause, each incident a bar where and as long as it happened (busy-line ones
// hatched, an ongoing one ringed); a bar opens the inspector.
const DAY = 86400;
function renderTimeline() {
  const box = document.getElementById('incidentTimeline');
  if (!box) return;
  const now = Date.now() / 1000;
  const midnight = new Date();
  midnight.setHours(24, 0, 0, 0); // the end of today, local time
  const end = midnight.getTime() / 1000;
  const start = end - 7 * DAY;
  const list = visibleIncidents().filter((i) => (i.end_time ?? now) >= start);
  const win = incidentWindow && incidentWindow.end > start ? incidentWindow : null;
  box.hidden = !list.length;
  if (!list.length) return;
  const pct = (t) => `${(((t - start) / (end - start)) * 100).toFixed(2)}%`;
  const lanes = CAUSES.filter(([key]) => list.some((i) => (i.cause || 'unknown') === key));
  const days = Array.from({ length: 7 }, (_, k) => new Date((start + k * DAY + 3600) * 1000).toLocaleDateString([], { weekday: 'short' }));
  box.innerHTML = lanes.map(([key, word]) => `<div class="tl-lane"><span class="tl-l">${word}</span><div class="tl-track">${list.filter((i) => (i.cause || 'unknown') === key).map((i) => {
    const level = levelOfIncident(i);
    const active = i.duration_seconds === null || i.duration_seconds === undefined;
    const t1 = i.end_time ?? now;
    const w = Math.max(0.45, ((t1 - i.start_time) / (end - start)) * 100);
    const label = `${i.title} · ${whenText(i.start_time)} · ${active ? 'ongoing' : durationOf(i).long}${i.expected ? ' · expected' : ''}`;
    return `<button type="button" class="tl-bar st-${level}${i.expected ? ' exp' : ''}${active ? ' ongoing' : ''}" style="left:${pct(Math.max(start, i.start_time))};width:${w.toFixed(2)}%" data-action="inspectIncident" data-arg="${i.id}" data-tip="${escapeHtml(label)}" aria-label="${escapeHtml(label.replace(/ · /g, ', '))}"></button>`;
  }).join('')}</div></div>`).join('')
    + `<div class="tl-axis"><span></span><div>${days.map((d) => `<span>${d}</span>`).join('')}</div></div>`
    + `<div class="tl-now" style="--now:${((now - start) / (end - start)).toFixed(4)}"></div>`
    + (win ? `<div class="tl-win" style="--a:${((Math.max(start, win.start) - start) / (end - start)).toFixed(4)};--b:${((win.end - start) / (end - start)).toFixed(4)}" aria-hidden="true"></div>` : '');
}

// The incidents overlapping a stretch of time, from a cell of another panel (When it breaks, Uptime): the list shows only
// those, the timeline marks the stretch, and the panel comes into view (on a phone its tab is opened first).
export function showIncidentWindow(win) {
  incidentWindow = win;
  incidentCurrentPage = 1;
  renderIncidentsPage();
  renderTimeline();
  if (!win) return;
  loadSince(win.start); // older than what is loaded: load back to it (list.js re-renders)
  const section = document.querySelector('[data-panel=incidents]');
  if (!section) return;
  const reveal = () => requestAnimationFrame(() => {
    const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    // the section is display: contents (style.css), so the card inside it is what has a place on the page
    (section.querySelector('.panel') || section).scrollIntoView({ block: 'start', behavior: smooth ? 'smooth' : 'auto' });
    document.getElementById('incidentWindow')?.focus({ preventScroll: true });
  });
  const tab = (section.dataset.tab || '').split(/\s+/)[0];
  // on a phone the panel's tab opens first; tabs.js shows it on hashchange, so the scroll waits for that
  if (section.hidden && tab && location.hash !== `#${tab}`) {
    window.addEventListener('hashchange', reveal, { once: true });
    location.hash = tab;
  } else reveal();
}

const overlaps = (inc, win) => inc.start_time < win.end && (inc.end_time ?? Date.now() / 1000) > win.start;

export function handleIncidentSearch(query) {
  incidentSearchQuery = (query || '').trim().toLowerCase();
  incidentCurrentPage = 1;
  renderIncidentsPage();
  if (incidentSearchQuery && hasMore()) loadAll(); // a search looks through everything the list can hold (list.js re-renders)
}

export function changeIncidentPageSize(val) {
  incidentPageSize = parseInt(val, 10) || 10;
  incidentCurrentPage = 1;
  renderIncidentsPage();
}

export function gotoIncidentPage(action, loaded = false) {
  const filtered = getFilteredIncidents();
  const totalPages = Math.max(1, Math.ceil(filtered.length / incidentPageSize));
  // past the last loaded page: load the older ones first (the last page is the oldest there is)
  if (!loaded && hasMore() && (action === 'last' || (action === 'next' && incidentCurrentPage >= totalPages))) {
    // a last page that was not full is filled first (the new rows continue it); stepping on would skip them
    const partial = action === 'next' && filtered.length < totalPages * incidentPageSize;
    (action === 'last' ? loadAll() : loadMore()).then(() => (partial ? renderIncidentsPage() : gotoIncidentPage(action, true)));
    return;
  }

  if (action === 'first') incidentCurrentPage = 1;
  else if (action === 'prev') incidentCurrentPage = Math.max(1, incidentCurrentPage - 1);
  else if (action === 'next') incidentCurrentPage = Math.min(totalPages, incidentCurrentPage + 1);
  else if (action === 'last') incidentCurrentPage = totalPages;
  else if (typeof action === 'number') incidentCurrentPage = Math.min(totalPages, Math.max(1, action));

  renderIncidentsPage();
}

// Incidents explained by heavy line load are hidden unless the toggle is on.
function visibleIncidents() {
  return showExpectedIncidents ? allRawIncidents : allRawIncidents.filter(inc => !inc.expected);
}

function getFilteredIncidents() {
  const base = incidentWindow ? visibleIncidents().filter((inc) => overlaps(inc, incidentWindow)) : visibleIncidents();
  if (!incidentSearchQuery) return base;
  return base.filter(inc => {
    return (
      (inc.title && inc.title.toLowerCase().includes(incidentSearchQuery)) ||
      (inc.description && inc.description.toLowerCase().includes(incidentSearchQuery)) ||
      (inc.severity && inc.severity.toLowerCase().includes(incidentSearchQuery)) ||
      (inc.cause_code && inc.cause_code.toLowerCase().includes(incidentSearchQuery))
    );
  });
}

function renderIncidentsPage() {
  const tbody = document.getElementById('incidentsBody');
  const countBadge = document.getElementById('incidentCountBadge');
  const pageInfo = document.getElementById('incidentPaginationInfo');
  const pageNum = document.getElementById('incidentPageNum');
  const btnFirst = document.getElementById('btnPageFirst');
  const btnPrev = document.getElementById('btnPagePrev');
  const btnNext = document.getElementById('btnPageNext');
  const btnLast = document.getElementById('btnPageLast');

  const filtered = getFilteredIncidents();
  const totalCount = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / incidentPageSize));

  if (incidentCurrentPage > totalPages) {
    incidentCurrentPage = totalPages;
  }

  // the server's count when it gave one, else what is loaded so far ("+" while there may be more)
  const known = totalCounts();
  // the total shown is that same count wherever a total appears, while more is still to load (unfiltered; a filter counts what matches)
  const unfiltered = !incidentSearchQuery && !incidentWindow;
  const knownShown = known ? (showExpectedIncidents ? known.total : known.total - known.expected) : null;
  const shownTotal = knownShown !== null && unfiltered && hasMore() ? Math.max(totalCount, knownShown) : totalCount;
  const expectedCount = known ? known.expected : allRawIncidents.filter(inc => inc.expected).length;
  if (countBadge) {
    countBadge.textContent = known ? `${showExpectedIncidents ? known.total : known.total - known.expected} recorded events`
      : `${visibleIncidents().length}${hasMore() ? '+' : ''} recorded events`;
  }

  const btnExpected = document.getElementById('btnToggleExpected');
  if (btnExpected) {
    btnExpected.hidden = expectedCount === 0;
    btnExpected.innerHTML = `${icon('busy')}${showExpectedIncidents ? 'Hide' : 'Show'} ${expectedCount} expected`;
    btnExpected.setAttribute('aria-pressed', String(showExpectedIncidents));
    btnExpected.title = 'Incidents explained by a busy line (expected, not a fault)';
  }

  const startIdx = (incidentCurrentPage - 1) * incidentPageSize;
  const endIdx = Math.min(startIdx + incidentPageSize, totalCount);
  const pageItems = filtered.slice(startIdx, endIdx);

  if (pageInfo) {
    if (totalCount === 0) {
      pageInfo.textContent = 'Showing 0 events';
    } else {
      pageInfo.textContent = `Showing ${startIdx + 1}–${endIdx} of ${shownTotal}${hasMore() && unfiltered && knownShown === null ? '+' : ''} events${incidentSearchQuery || incidentWindow ? ' (filtered)' : ''}`;
    }
  }

  if (pageNum) {
    pageNum.textContent = `Page ${incidentCurrentPage} of ${totalPages}`;
  }

  if (btnFirst) btnFirst.disabled = (incidentCurrentPage <= 1);
  if (btnPrev) btnPrev.disabled = (incidentCurrentPage <= 1);
  // more is loaded when the last loaded page is passed
  if (btnNext) btnNext.disabled = (incidentCurrentPage >= totalPages && !hasMore());
  if (btnLast) btnLast.disabled = (incidentCurrentPage >= totalPages && !hasMore());

  renderWindowBar(totalCount);
  if (!tbody) return;

  if (pageItems.length === 0) {
    const [head, text] = incidentSearchQuery ? ['Nothing to show', 'No incident matches the filter.']
      : incidentWindow ? emptyWindowText() : ['Nothing to show', 'No network incidents recorded.'];
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty">${icon('ok')}<div><b>${escapeHtml(head)}</b>${escapeHtml(text)}</div></div></td></tr>`;
    return;
  }

  tbody.replaceChildren(...pageItems.map(incidentRow));
}

function renderWindowBar(found) {
  const bar = document.getElementById('incidentWindow');
  if (!bar) return;
  bar.hidden = !incidentWindow;
  if (!incidentWindow) return;
  bar.querySelector('[data-f=label]').textContent = incidentWindow.label;
  bar.querySelector('[data-f=found]').textContent = found === 1 ? '1 incident' : `${found} incidents`;
}

// Why a window has no incident to list: busy-line ones hidden by the toggle, older than the loaded list, nothing probed
// then, nothing lost, or loss too small or brief to open one. `lost` / `sent` come from an hour of When it breaks,
// `state` from an Uptime block (that one target's block); either may be missing.
function emptyWindowText() {
  const win = incidentWindow;
  const hidden = showExpectedIncidents ? 0 : allRawIncidents.filter((inc) => inc.expected && overlaps(inc, win)).length;
  if (hidden) return ['Only busy-line events here', `${hidden === 1 ? 'One incident' : `${hidden} incidents`} in this time ${hidden === 1 ? 'was' : 'were'} explained by a busy line. Show expected to list ${hidden === 1 ? 'it' : 'them'}.`];
  if (isLoading() && hasMore() && win.start < (oldestLoaded() ?? Infinity)) return ['Loading', 'Looking for older incidents.'];
  const oldest = !hasMore() && allRawIncidents.length >= CAP ? oldestLoaded() : null;
  if (oldest !== null && win.start < oldest) return ['Older than the list', `Only the newest ${CAP} incidents are loaded, and this time is before them.`];
  if (win.sent === 0 || win.state === 'no_data') return ['No probes in this time', 'Nothing was probed then (Pengumon was not running), so there is nothing to list.'];
  if (win.sent > 0 && win.lost === 0) return ['Nothing failed in this time', `All ${win.sent.toLocaleString()} probes answered and no incident was open.`];
  if (win.state === 'ok') return ['Nothing failed in this time', 'No incident was open, and this target lost no probes.'];
  const lost = win.lost ? `${win.lost.toLocaleString()} of ${win.sent.toLocaleString()} probes were lost, ` : 'The probes lost here were ';
  return ['No incident in this time', `${lost}too few or too brief to count as an incident (short drops are still in the loss figures).`];
}

// Where an incident's fault sat, as a word (js/path-api.js CAUSES, in the timeline's lane order).
export const causeOf = (inc) => CAUSE_SHORT[inc.cause] || 'Other';

export function levelOfIncident(inc) {
  if (inc.expected) return 'busy';
  return inc.severity === 'critical' ? 'crit' : 'warn';
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(Math.round(seconds % 60)).padStart(2, '0')}s`;
  return `${Math.floor(seconds / 3600)}h ${String(Math.round((seconds % 3600) / 60)).padStart(2, '0')}m`;
}

// How long an incident lasted, as people felt it: the loss span (the server's loss_seconds, first to last lost probe plus
// one probe interval) when probes were lost, else the recorded duration. The recorded one runs from detection to
// recovery, a few seconds after the loss on both ends; it is the hover text, and the inspector shows both. Old
// incidents (no samples to read the loss from) and those raised on latency (nothing lost) show the recorded duration.
// {text: the short form, long: "Loss 7s · incident 12s", title: the explanation, lost: whether a loss span is shown}.
export function durationOf(inc) {
  const open = inc.duration_seconds === null || inc.duration_seconds === undefined;
  if (open) return { text: 'active', long: 'in progress', title: '', lost: false };
  const recorded = formatDuration(inc.duration_seconds);
  if (!(inc.loss_seconds > 0)) {
    const why = inc.loss_seconds === 0 ? 'no probe was lost (raised on latency)' : 'no stored probes to read the loss from';
    return { text: recorded, long: recorded, title: `Incident open ${recorded}, from detection to recovery; ${why}`, lost: false };
  }
  const lost = formatDuration(inc.loss_seconds);
  return { text: `${lost} lost`, long: `Loss ${lost} · incident ${recorded}`, lost: true,
    title: `Probes lost for ${lost} (first to last lost probe). The incident was open ${recorded}: it is detected a few seconds after the loss starts and closes a few seconds after it ends.` };
}

export function whenText(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === today.toDateString()) return `Today ${time}`;
  if (today - d < 6 * 86400000) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

// The load figure of an incident as a chip: the peak WAN load, or the words saying the recorded figure is unusable.
export function loadChip(inc) {
  return inc.load_pct === null || inc.load_pct === undefined ? ''
    : inc.load_suspect ? ' <span class="chip" title="The WAN rate recorded with this incident was impossible for the line; the figure is not used">load: not a valid figure</span>'
      : ` <span class="chip" title="Peak WAN load around this event, % of line capacity">${Math.round(inc.load_pct)}% load</span>`;
}

function incidentRow(inc) {
  const tr = document.createElement('tr');
  tr.className = inc.expected ? 'row-click incident-row expected' : 'row-click incident-row';
  tr.dataset.action = 'inspectIncident';
  tr.dataset.arg = inc.id;
  const level = levelOfIncident(inc);
  const active = inc.duration_seconds === null || inc.duration_seconds === undefined;
  const dur = durationOf(inc);
  const status = active ? badge(level, 'Ongoing') : badge(level, inc.expected ? 'Expected' : level === 'crit' ? 'Outage' : 'Degraded', 'quiet');
  // the title is the row's button, so the keyboard reaches it; a click anywhere on the row does the same
  tr.innerHTML = `
    <td class="lead"><button type="button" class="primary" data-action="inspectIncident" data-arg="${inc.id}" title="${escapeHtml(inc.title)}: open the details">${escapeHtml(inc.title)}</button></td>
    <td class="aside">${status}</td>
    <td class="num" data-l=""><span class="when" title="${escapeHtml(new Date(inc.start_time * 1000).toLocaleString())}">${escapeHtml(whenText(inc.start_time))}</span></td>
    <td class="num r"><span class="dur${dur.lost ? ' lost' : ''}"${dur.title ? ` title="${escapeHtml(dur.title)}"` : ''}>${escapeHtml(dur.text)}</span></td>
    <td class="hide-narrow"><span class="chip">${escapeHtml(causeOf(inc))}</span></td>
    <td class="r hide-narrow go" aria-hidden="true">${icon('right')}</td>`;
  return tr;
}

// A probe count from a cell's data-* attribute, left out when the cell does not carry one.
const count = (el, key) => (el.dataset[key] === undefined ? {} : { [key]: Number(el.dataset[key]) });

// The actions this module's markup names (see dispatch.js).
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
