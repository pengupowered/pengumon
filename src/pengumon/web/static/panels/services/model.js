// Host services card, the model: the apps on this host whose health pengumon checks (GET /api/services), how to open
// them and their recent outages. Shown only when the host_services feature is available. Polled every 30 s; nothing
// here rides the websocket. present() turns the services into the view model every view of the card draws
// (view.js is the standard one; docs/themes.md, "Drawing a card"); the header's summary and the card's status are the
// frame, kept here.

import { createCard } from '../../js/cards.js';
import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { copyText } from '../../js/clipboard.js';
import { localFormat } from '../../js/clock.js';
import { STATUS } from '../../js/dom.js';
import standard from './view.js';
import { everyWhileVisible } from '../../js/timers.js';

const POLL_MS = 30000;
const REDRAW_MS = 5000; // keeps "checked Ns ago" honest between polls
const LEVELS = { up: 'good', degraded: 'warn', down: 'crit', unknown: 'unknown' };
const LABELS = { up: 'Up', degraded: 'Degraded', down: 'Down', unknown: 'Checking' };
const COPY_LABEL = 'Copy the health-check address';

let services = [];
let card = null;
const openHistory = new Set(); // ids whose History is expanded; survives redraws and theme changes

function ago(ts, now) {
  if (ts === null || ts === undefined) return null;
  const s = Math.max(0, Math.round(now - ts));
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

// One service as the views draw it: every value formatted, the status as level + word + icon, the actions.
function row(svc, open, now) {
  const level = LEVELS[svc.status] || 'unknown';
  const failing = svc.status !== 'up' && svc.status !== 'unknown' && svc.error ? svc.error : null;
  const url = safeHttpUrl(svc.open_url);
  const facts = [];
  const checked = ago(svc.last_checked, now);
  facts.push({ key: 'checked', text: checked ? `checked ${checked} ago` : 'not checked yet' });
  if (svc.since !== null && svc.since !== undefined && svc.status !== 'unknown') {
    facts.push({ key: 'since', text: `${svc.status === 'up' ? 'up' : 'failing'} since ${ago(svc.since, now)} ago` });
  }
  if (svc.status !== 'up' && svc.last_ok) facts.push({ key: 'lastOk', text: `last answered ${ago(svc.last_ok, now)} ago` });
  const expanded = open.has(svc.id);
  return {
    id: svc.id,
    status: { level, word: LABELS[svc.status] || svc.status, icon: STATUS[level].icon },
    name: svc.name,
    reason: failing ? { text: failing.message, kind: failing.kind } : null,
    checkUrl: svc.check_url_display,
    ms: svc.latency_ms !== null && svc.latency_ms !== undefined && !failing ? `${Math.round(svc.latency_ms)} ms` : '–',
    description: svc.description || '',
    facts,
    open: url ? { href: url, label: `Open ${svc.name}`, icon: 'external' } : null,
    copy: { action: 'copyServiceUrl', arg: svc.check_url_display, label: COPY_LABEL, icon: 'copy' },
    history: { action: 'toggleServiceHistory', arg: svc.id, label: expanded ? 'Hide history' : 'History', icon: 'history', expanded },
    // the outages, only while History is open: [] says "No outages recorded."
    outages: expanded ? (svc.incidents || []).map((inc) => ({
      when: localFormat(inc.start_time * 1000, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
      length: inc.resolved ? duration(inc.duration_seconds) : 'ongoing',
      text: inc.description
    })) : null
  };
}

// The card's status: the worst of its services (a degraded one is a warning, only a down one an outage).
export function cardStatus(list) {
  if (list.some((s) => s.status === 'down')) return 'crit';
  return list.some((s) => s.status === 'degraded') ? 'warn' : 'good';
}

// The view model: { rows: [row...], summary, status } (summary and status are the frame's, kept by render below).
export function present(list, open, now) {
  const down = list.filter((s) => s.status === 'down' || s.status === 'degraded').length;
  return {
    rows: list.map((svc) => row(svc, open, now)),
    summary: down ? `${down} of ${list.length} not answering` : `${list.length} apps on this host`,
    status: cardStatus(list)
  };
}

// Made-up services for the parity test of the card's views (tests/ui/steps/core.mjs): an up one with a link, a down one
// with its History open and an outage, one not checked yet.
export function fixture() {
  const now = 1767225600; // a fixed moment, so "checked 12s ago" reads the same every time
  const list = [
    { id: 'fx_up', name: 'Example app', description: 'The example app', status: 'up', latency_ms: 12.4, error: null,
      check_url_display: '192.0.2.10:8080/health', open_url: 'http://app.example.com/', last_checked: now - 12, since: now - 7200,
      last_ok: now - 12, incidents: [] },
    { id: 'fx_down', name: 'Example store', description: '', status: 'down', latency_ms: null,
      error: { kind: 'refused', message: 'connection refused (stopped or restarting)' }, check_url_display: '192.0.2.11:9000/api/health',
      open_url: null, last_checked: now - 3, since: now - 600, last_ok: now - 660,
      incidents: [{ start_time: now - 600, resolved: false, duration_seconds: null, description: 'Example store not responding' },
        { start_time: now - 86400, resolved: true, duration_seconds: 300, description: 'Example store not responding' }] },
    { id: 'fx_new', name: 'Example queue', description: '', status: 'unknown', latency_ms: null, error: null,
      check_url_display: '192.0.2.12/health', open_url: 'https://queue.example.com/', last_checked: null, since: null, last_ok: null, incidents: [] }
  ];
  return present(list, new Set(['fx_down']), now);
}

// The frame (header summary, the card's status) and the body, from the services now.
function render() {
  if (!card) return;
  const vm = present(services, openHistory, Date.now() / 1000);
  card.update(vm);
  const summary = document.getElementById('servicesSummary');
  if (summary) summary.textContent = vm.summary;
  const frame = document.getElementById('servicesCard');
  if (frame.dataset.status !== vm.status) frame.dataset.status = vm.status; // (the same value written again would wake the tab-dot observer)
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

function toggleServiceHistory(id) {
  if (openHistory.has(id)) openHistory.delete(id); else openHistory.add(id);
  render();
}

function copyServiceUrl(text, button) {
  if (!text) return;
  const done = (label) => {
    if (!button) return;
    button.title = label;
    button.querySelector('use')?.setAttribute('href', '#i-ok');
    setTimeout(() => { button.title = COPY_LABEL; button.querySelector('use')?.setAttribute('href', '#i-copy'); }, 2000);
  };
  copyText(text).then((copied) => { if (copied) done('Copied'); else prompt('Copy address:', text); });
}

// Starts only when the host_services feature is available; otherwise nothing is fetched (tabs.js shows or hides the card).
export function initServices() {
  const feature = state.features.find((f) => f.feature === 'host_services');
  const article = document.getElementById('servicesCard');
  if (!article || !feature || !feature.available) return;
  card = createCard({ panel: 'services', root: article.querySelector('.panel-b'), standard, fixture });
  refresh();
  everyWhileVisible(refresh, POLL_MS);
  everyWhileVisible(render, REDRAW_MS);
  document.addEventListener('zonechange', render); // the outages' times are on the page's clock
}

// The actions this card's views name (see dispatch.js).
registerActions('click', {
  copyServiceUrl: (el) => copyServiceUrl(el.dataset.arg, el),
  toggleServiceHistory: (el) => toggleServiceHistory(el.dataset.arg)
});
