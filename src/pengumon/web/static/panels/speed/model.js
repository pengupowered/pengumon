// Speed card, the model: the scheduled speed test (GET /api/speedtests): the last result against the plan, the grade
// for delay under load, a history chart (7 or 30 days), when the next test is due, this month's data use, and a Run now
// button that asks first (it uses data). Polled every minute, and every 3 s while a test runs. present() turns the
// answer into the view model every view of the card draws (view.js is the standard one; docs/themes.md, "Drawing a
// card"); the header's summary and the card's status are the frame, kept here.

import { createCard } from '../../js/cards.js';
import { registerActions } from '../../js/dispatch.js';
import { settingsValues, val } from '../../js/settings-values.js';
import standard from './view.js';
import { chartOf, lastResult, nextText, PROVIDERS, problem, runBlock, usageText } from './result.js';
import { everyWhileVisible, whenVisible } from '../../js/timers.js';

const POLL_MS = 60000;
const RUNNING_POLL_MS = 3000;
const REDRAW_MS = 30000; // keeps "3 h ago" honest between polls

let data = null; // the last answer of the server, null until it came
let failed = false; // the last request failed
let range = 7;
let confirming = false;
let starting = false; // the POST is on its way
let note = null; // {level, text}: why Run now did not start a test
let card = null;
let pollTimer = null;

// The data a test may use, from the settings (the answer has no sizes): "Uses up to 25 MB down and 10 MB up of data."
function dataText() {
  const v = settingsValues();
  return `Uses up to ${val(v, 'speedtest.download_mb', 25)} MB down and ${val(v, 'speedtest.upload_mb', 10)} MB up of data.`;
}

// The card's status: good while the newest test worked; a test that did not work, or none, is unknown, not a warning
// (a warning puts a dot on the Network tab of a phone, and a provider that refuses one test is no fault of the line).
export function cardStatus(data) {
  const t = data && data.enabled ? data.tests[0] : null;
  return t && t.status === 'ok' ? 'good' : 'unknown';
}

// The view model: { state: 'loading' | 'error' | 'ready', error, summary, status, off, empty, problem, last, chart, next,
// usage, run, confirm, progress, note }. (summary and status are the frame's, kept by render below.)
export function present(d, ui, now) {
  if (!d) {
    return failed ? { state: 'error', error: 'Could not load the speed tests.', summary: 'unavailable', status: 'unknown' }
      : { state: 'loading', summary: 'loading...', status: 'unknown' };
  }
  const block = runBlock(d);
  const settings = { action: 'openSettingsAt', arg: 'speedtest', label: 'Speed test settings', icon: 'settings' };
  const last = lastResult(d, now);
  const running = d.running || ui.starting;
  const off = !d.enabled;
  let empty = null;
  if (!last && !problem(d, now)) {
    empty = off ? 'Speed tests are off. Turn them on to see how fast your line really is, and when it slows down.'
      : 'No speed test has run yet. The first one runs on its schedule, or press Run now.';
  }
  return {
    state: 'ready',
    summary: off ? 'off' : (PROVIDERS[d.provider] || d.provider || 'on'),
    status: cardStatus(d),
    off: off ? { text: 'Speed test is off', link: { ...settings, label: 'Turn on in Settings' } } : null,
    empty,
    problem: problem(d, now),
    last,
    chart: empty ? null : chartOf(d, now, range),
    next: nextText(d, now),
    usage: usageText(d),
    usageLevel: d.monthly_cap_mb > 0 && d.month_used_mb >= d.monthly_cap_mb ? 'warn' : null,
    run: {
      action: 'speedAskRun', label: 'Run now', icon: 'refresh', needsEdit: true,
      disabled: !!block || ui.confirming || ui.starting, title: block || (running ? 'A speed test is running.' : 'Run a speed test now')
    },
    confirm: ui.confirming && !block ? {
      title: 'Run a speed test now?', text: dataText(),
      yes: { action: 'speedRunConfirm', label: 'Run test', icon: 'refresh', needsEdit: true },
      no: { action: 'speedRunCancel', label: 'Cancel' }
    } : null,
    progress: running ? { text: 'Testing download, then upload...', detail: 'This takes up to half a minute.' } : null,
    note: ui.note && !running ? ui.note : null
  };
}

// Made-up answer for the parity test of the card's views: a good result the router measured against a plan with a grade,
// a partial one, a failed and a skipped one, the card asking to confirm Run now.
export function fixture() {
  const now = 1767225600; // a fixed moment, so "3 h ago" reads the same every time
  const row = (id, ago, over) => ({ id, ts: now - ago, duration_s: 24, trigger: 'scheduled', provider: 'cloudflare', server: 'EXA', status: 'ok', reason: null,
    down_mbps: 240.4, up_mbps: 18.2, idle_ms: 12, loaded_down_ms: 60, loaded_up_ms: 25, bytes_down: 250000000, bytes_up: 20000000, bufferbloat: 'B',
    wan_down_mbps: 281.7, wan_up_mbps: 19.6, headline_down_mbps: 281.7, headline_up_mbps: 19.6, down_measured_by: 'router', up_measured_by: 'router', measured_by: 'router', traffic_note: null, ...over });
  const tests = [row(4, 3 * 3600, {}), row(3, 27 * 3600, { status: 'failed', reason: 'download: the provider refused (rate limit)', down_mbps: null, up_mbps: null, bufferbloat: null, idle_ms: null }),
    row(2, 51 * 3600, { down_mbps: 180.9, up_mbps: 17.5 }), row(1, 75 * 3600, { status: 'skipped', reason: 'line busy', down_mbps: null, up_mbps: null, bufferbloat: null, idle_ms: null })];
  const answer = { enabled: true, provider: 'cloudflare', next_run: now + 5 * 3600, running: false, month_used_mb: 410, monthly_cap_mb: 2000,
    plan: { down_mbps: 300, up_mbps: 20 }, tests };
  return present(answer, { confirming: true, starting: false, note: null }, now);
}

function render() {
  if (!card) return;
  const vm = present(data, { confirming, starting, note }, Date.now() / 1000);
  card.update(vm);
  const summary = document.getElementById('speedSummary');
  if (summary) summary.textContent = vm.summary;
  const frame = document.getElementById('speedCard');
  if (frame.dataset.status !== vm.status) frame.dataset.status = vm.status; // (the same value written again would wake the tab-dot observer)
}

// Polls again soon while a test runs, else once a minute.
function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => whenVisible(refresh), data && (data.running || starting) ? RUNNING_POLL_MS : POLL_MS);
}

export async function refresh() {
  try {
    const res = await fetch('/api/speedtests?hours=720&limit=500');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    data = body && Array.isArray(body.tests) ? body : null;
    failed = !data;
    if (data && data.running) starting = false; // the server has it now
  } catch (e) {
    console.warn('Failed to load /api/speedtests:', e);
    failed = data === null;
  }
  render();
  schedule();
}

async function runNow() {
  confirming = false;
  starting = true;
  note = null;
  render();
  try {
    const res = await fetch('/api/speedtests/run', { method: 'POST' });
    if (res.status === 202) {
      await refresh();
      starting = false; // a test the server already finished is in the answer; one it still runs shows as running
      render();
      return;
    }
    let reason = `The speed test did not start (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (body && body.reason) reason = `The speed test did not start: ${body.reason}.`;
    } catch (e) { /* keep the status text */ }
    note = { level: 'warn', text: reason };
  } catch (e) {
    note = { level: 'warn', text: 'The speed test did not start: the server could not be reached.' };
  }
  starting = false;
  render();
  schedule();
}

export function initSpeed() {
  const article = document.getElementById('speedCard');
  if (!article) return;
  card = createCard({ panel: 'speed', root: article.querySelector('.panel-b'), standard, fixture });
  render();
  refresh();
  everyWhileVisible(render, REDRAW_MS);
  document.addEventListener('zonechange', render);
}

// The actions this card's views name (see dispatch.js).
registerActions('click', {
  speedAskRun: () => { confirming = true; note = null; render(); },
  speedRunCancel: () => { confirming = false; render(); },
  speedRunConfirm: () => { runNow(); },
  speedRange: (el) => { range = Number(el.dataset.arg) === 30 ? 30 : 7; render(); }
});
