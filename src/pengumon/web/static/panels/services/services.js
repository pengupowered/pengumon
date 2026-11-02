// Host services card: the apps on this host whose health pengumon checks (GET /api/services), how to open them and
// their recent outages. Shown only when the host_services feature is available. Polled every 30 s; nothing here
// rides the websocket. Everything comes from the API as text and is placed with textContent / DOM APIs.

import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { icon, STATUS } from '../../js/util.js';

const POLL_MS = 30000;
const REDRAW_MS = 5000; // keeps "checked Ns ago" honest between polls
const LEVELS = { up: 'good', degraded: 'warn', down: 'crit', unknown: 'unknown' };
const LABELS = { up: 'Up', degraded: 'Degraded', down: 'Down', unknown: 'Checking' };

let services = [];
const openHistory = new Set(); // ids whose History is expanded; survives redraws

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function ago(ts) {
  if (ts === null || ts === undefined) return null;
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${Math.round(s / 86400)}d`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return 'ongoing';
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// Only an http(s) address becomes a link (the server validates it as well); anything else shows as nothing.
function safeHttpUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch (e) {
    return null;
  }
}

function historyList(svc) {
  const box = el('div', 'svc-history');
  box.dataset.role = 'history';
  if (!svc.incidents.length) {
    box.appendChild(el('span', null, 'No outages recorded.'));
    return box;
  }
  svc.incidents.forEach((inc) => {
    const line = el('div', 'svc-history-item');
    const when = new Date(inc.start_time * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
    line.appendChild(el('strong', null, when));
    line.appendChild(el('span', null, ` ${inc.resolved ? duration(inc.duration_seconds) : 'ongoing'} `));
    line.appendChild(el('span', null, inc.description));
    box.appendChild(line);
  });
  return box;
}

function iconButton(name, label, action, arg) {
  const b = el('button', 'btn icon sm');
  b.type = 'button';
  b.innerHTML = icon(name);
  b.title = label;
  b.setAttribute('aria-label', label);
  b.dataset.action = action;
  b.dataset.arg = arg;
  return b;
}

function renderRow(svc) {
  const level = LEVELS[svc.status] || 'unknown';
  const row = el('li', `svc-row st-${level}`);
  row.dataset.service = svc.id;

  const st = el('span', 'svc-st');
  st.innerHTML = icon(STATUS[level].icon);
  const word = st.appendChild(el('span', null, LABELS[svc.status] || svc.status));
  word.dataset.role = 'status';
  row.appendChild(st);

  const id = el('span', 'svc-id svc-head');
  id.appendChild(el('b', 'svc-name', svc.name));
  const failing = svc.status !== 'up' && svc.status !== 'unknown' && svc.error;
  if (failing) {
    const why = el('span', 'svc-why svc-reason', svc.error.message);
    why.dataset.kind = svc.error.kind;
    id.appendChild(why);
  } else {
    id.appendChild(el('span', 'mono svc-check-url', svc.check_url_display));
  }
  row.appendChild(id);
  const ms = svc.latency_ms !== null && svc.latency_ms !== undefined && !failing ? `${Math.round(svc.latency_ms)} ms` : '–';
  row.appendChild(el('span', 'svc-ms num svc-ms-col', ms));

  const actions = el('span', 'svc-actions');
  const url = safeHttpUrl(svc.open_url);
  if (url) {
    const link = document.createElement('a');
    link.className = 'btn icon sm svc-open';
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.title = `Open ${svc.name}`;
    link.setAttribute('aria-label', `Open ${svc.name}`);
    link.innerHTML = icon('external');
    actions.appendChild(link);
  }
  actions.appendChild(iconButton('copy', 'Copy the health-check address', 'copyServiceUrl', svc.check_url_display));
  const history = iconButton('history', openHistory.has(svc.id) ? 'Hide history' : 'History', 'toggleServiceHistory', svc.id);
  history.setAttribute('aria-expanded', String(openHistory.has(svc.id)));
  actions.appendChild(history);
  row.appendChild(actions);

  const facts = [];
  const checked = ago(svc.last_checked);
  facts.push(checked ? `checked ${checked} ago` : 'not checked yet');
  if (svc.since !== null && svc.since !== undefined && svc.status !== 'unknown') {
    facts.push(`${svc.status === 'up' ? 'up' : 'failing'} since ${ago(svc.since)} ago`);
  }
  if (svc.status !== 'up' && svc.last_ok) facts.push(`last answered ${ago(svc.last_ok)} ago`);
  const more = el('div', 'svc-more');
  if (svc.description) more.appendChild(el('span', null, svc.description));
  more.appendChild(el('span', 'svc-facts', facts.join(' · ')));
  if (failing) more.appendChild(el('span', 'mono svc-check-url', svc.check_url_display));
  if (openHistory.has(svc.id)) more.appendChild(historyList(svc));
  row.appendChild(more);
  return row;
}

function render() {
  const list = document.getElementById('servicesList');
  if (!list) return;
  list.replaceChildren(...services.map(renderRow));
  const down = services.filter((s) => s.status === 'down' || s.status === 'degraded').length;
  const summary = document.getElementById('servicesSummary');
  if (summary) summary.textContent = down ? `${down} of ${services.length} not answering` : `${services.length} apps on this host`;
  const card = document.getElementById('servicesCard');
  if (card) card.dataset.status = down ? 'crit' : 'good';
}

async function refresh() {
  try {
    const res = await fetch('/api/services');
    if (!res.ok) return;
    const data = await res.json();
    services = Array.isArray(data) ? data : [];
    render();
  } catch (e) {
    console.warn('Failed to load /api/services:', e);
  }
}

export function toggleServiceHistory(id) {
  if (openHistory.has(id)) openHistory.delete(id); else openHistory.add(id);
  render();
}

export function copyServiceUrl(text, button) {
  if (!text) return;
  const done = (label) => {
    if (!button) return;
    button.title = label;
    button.querySelector('use')?.setAttribute('href', '#i-ok');
    setTimeout(() => { button.title = 'Copy the health-check address'; button.querySelector('use')?.setAttribute('href', '#i-copy'); }, 2000);
  };
  navigator.clipboard.writeText(text).then(() => done('Copied')).catch(() => {
    prompt('Copy address:', text);
  });
}

// Starts only when the host_services feature is available; otherwise nothing is fetched (tabs.js shows or hides the card).
export function initServices() {
  const feature = state.features.find((f) => f.feature === 'host_services');
  const card = document.getElementById('servicesCard');
  if (!card || !feature || !feature.available) return;
  refresh();
  setInterval(refresh, POLL_MS);
  setInterval(render, REDRAW_MS);
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  copyServiceUrl: (el) => copyServiceUrl(el.dataset.arg, el),
  toggleServiceHistory: (el) => toggleServiceHistory(el.dataset.arg)
});
