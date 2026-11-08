// Speed card, the model: the scheduled speed test (GET /api/speedtests): the last result against the plan, the grade
// for delay under load, a history chart (7 or 30 days), when the next test is due, this month's data use, and a Run now
// button that asks first (it uses data). Polled every minute, and every 3 s while a test runs. present() turns the
// answer into the view model every view of the card draws (view.js is the standard one; docs/themes.md, "Drawing a
// card"); the header's summary and the card's status are the frame, kept here.

import { createCard } from '../../js/cards.js';
import { registerActions } from '../../js/dispatch.js';
import { settingsValues, val } from '../../js/settings-values.js';
import { localFormat, STATUS } from '../../js/util.js';
import standard from './view.js';

const POLL_MS = 60000;
const RUNNING_POLL_MS = 3000;
const REDRAW_MS = 30000; // keeps "3 h ago" honest between polls
const PROVIDERS = { cloudflare: 'Cloudflare', mlab: 'M-Lab', librespeed: 'LibreSpeed' };
const RANGES = [{ days: 7, label: '7 d' }, { days: 30, label: '30 d' }];
// A result's status as the card says it (never color alone: icon and word).
const RESULT = {
  ok: { level: 'good', word: 'Done' },
  partial: { level: 'warn', word: 'Partial' },
  failed: { level: 'serious', word: 'Failed' },
  skipped: { level: 'unknown', word: 'Skipped' }
};
const GRADE_LEVEL = { 'A+': 'good', A: 'good', B: 'warn', C: 'warn', D: 'serious', F: 'crit' };

let data = null; // the last answer of the server, null until it came
let failed = false; // the last request failed
let range = 7;
let confirming = false;
let starting = false; // the POST is on its way
let note = null; // {level, text}: why Run now did not start a test
let card = null;
let pollTimer = null;

const has = (v) => v !== null && v !== undefined;
const fmtInt = (n) => Math.round(n).toLocaleString('en-US');

// 212.4 -> "212", 8.46 -> "8.5": three figures are plenty for a speed
export function mbps(v) {
  if (!has(v)) return '–';
  return v >= 100 ? fmtInt(v) : (Math.round(v * 10) / 10).toString();
}

// seconds -> "5 min", "3 h", "2 d"
function span(seconds) {
  const s = Math.max(0, seconds);
  if (s < 90) return 'a minute';
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

export function agoText(ts, now) {
  const s = now - ts;
  return s < 90 ? 'just now' : `${span(s)} ago`;
}

export function nextText(data, now) {
  if (!data.enabled) return null;
  if (data.running) return 'Test running now';
  if (!has(data.next_run)) return null;
  const s = data.next_run - now;
  return s <= 60 ? 'Next test due now' : `Next test in ${span(s)}`;
}

function usageText(data) {
  const used = data.month_used_mb;
  if (data.monthly_cap_mb > 0) return `${fmtInt(used)} MB of ${fmtInt(data.monthly_cap_mb)} MB this month`;
  return `${fmtInt(used)} MB this month`;
}

const planText = (value, plan) => (has(value) && plan > 0 ? `${Math.round((value / plan) * 100)} % of ${fmtInt(plan)} Mbps` : null);

function server(t, providerName) {
  return t.server ? `${providerName} (${t.server})` : providerName;
}

// The delay under load: "idle 12 ms, under load +48 ms" (the larger of the two loaded medians less the idle one).
function bufferbloat(t) {
  if (!t.bufferbloat) return null;
  const loaded = [t.loaded_down_ms, t.loaded_up_ms].filter(has);
  const added = Math.max(0, Math.max(...loaded) - t.idle_ms);
  const parts = [];
  if (has(t.loaded_down_ms)) parts.push(`download ${Math.round(t.loaded_down_ms)} ms`);
  if (has(t.loaded_up_ms)) parts.push(`upload ${Math.round(t.loaded_up_ms)} ms`);
  const level = GRADE_LEVEL[t.bufferbloat] || 'unknown';
  return {
    grade: t.bufferbloat,
    status: { level, word: `Grade ${t.bufferbloat}`, icon: STATUS[level].icon },
    text: `idle ${Math.round(t.idle_ms)} ms, under load +${Math.round(added)} ms`,
    title: `Delay while the line is busy: idle ${Math.round(t.idle_ms)} ms; ${parts.join(', ')}. The grade follows the added delay (A+ under 5 ms, A under 30, B under 60, C under 200, D under 400, F beyond).`
  };
}

// A test's headline speed of one direction (the server gives it: the line's rate the router measured, else the test's
// own), and the test's own (kept beside it).
const best = (t, dir) => (has(t[`headline_${dir}_mbps`]) ? t[`headline_${dir}_mbps`] : t[`${dir}_mbps`]);

// How the big numbers were measured: by the router (what the line carried, the household's traffic included) or by the
// test alone (one server, which may not fill the line).
function measuredBy(t) {
  if (t.measured_by === 'router') {
    return { text: 'Line, measured by your router', title: 'What the line carried while the test filled it, read from your router (the test and any other traffic). The test\'s own figure is under each number.' };
  }
  return { text: 'Test only', title: 'What one test server delivered to this computer. A router that reports WAN counters would measure the line itself (Settings > Devices).' };
}

// The test's own figure under a headline the router measured: "test alone 567 Mbps".
function own(t, dir) {
  return t[`${dir}_measured_by`] === 'router' && has(t[`${dir}_mbps`]) ? `test alone ${mbps(t[`${dir}_mbps`])} Mbps` : null;
}

// The result shown in the big numbers: the newest test that measured anything.
function lastResult(data, now) {
  const tests = data.tests;
  const t = tests.find((x) => has(x.down_mbps) || has(x.up_mbps));
  if (!t) return null;
  const name = PROVIDERS[t.provider] || t.provider;
  const status = RESULT[t.status] || RESULT.failed;
  const down = best(t, 'down');
  const up = best(t, 'up');
  return {
    id: t.id,
    down: { num: mbps(down), unit: 'Mbps', plan: planText(down, data.plan.down_mbps), own: own(t, 'down') },
    up: { num: mbps(up), unit: 'Mbps', plan: planText(up, data.plan.up_mbps), own: own(t, 'up') },
    by: measuredBy(t),
    // the household's traffic shared the line: the test's own figure is far under the line's
    traffic: t.traffic_note || null,
    when: { text: agoText(t.ts, now), title: localFormat(t.ts * 1000, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) },
    server: server(t, name),
    // a measured result that is not whole says why: "Partial: upload: the server refused"
    status: t.status === 'ok' ? null : { level: status.level, word: status.word, icon: STATUS[status.level].icon, reason: t.reason || null },
    // a per-result remark ("too little data to measure a line this fast"), when the server gives one
    note: t.note || null,
    bufferbloat: bufferbloat(t)
  };
}

// The newest test when it measured nothing (failed or skipped): said above the last good result, or on its own.
function problem(data, now) {
  const t = data.tests[0];
  if (!t || has(t.down_mbps) || has(t.up_mbps)) return null;
  const status = RESULT[t.status] || RESULT.failed;
  return {
    status: { level: status.level, word: status.word, icon: STATUS[status.level].icon },
    when: { text: agoText(t.ts, now), title: localFormat(t.ts * 1000, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) },
    reason: t.reason || (t.status === 'skipped' ? 'The test did not run.' : 'The test did not finish.')
  };
}

// The history chart's data: plain numbers (times in ms); the view gives them colors.
function chartOf(data, now) {
  const edge = Math.ceil(now / 600) * 600; // the chart's right edge moves every 10 minutes, so a redraw in between changes nothing
  const from = edge - range * 86400;
  const tests = data.tests.filter((t) => t.ts >= from).slice().reverse();
  if (!tests.length) return null;
  const point = (t, dir) => ({ x: t.ts * 1000, y: best(t, dir) });
  const down = tests.filter((t) => has(best(t, 'down'))).map((t) => point(t, 'down'));
  const up = tests.filter((t) => has(best(t, 'up'))).map((t) => point(t, 'up'));
  if (!down.length && !up.length) return null; // marks alone are no chart
  const mark = (status) => tests.filter((t) => t.status === status).map((t) => ({ x: t.ts * 1000, y: 0, tip: `${RESULT[status].word}${t.reason ? `: ${t.reason}` : ''}` }));
  const bad = mark('failed');
  const skipped = mark('skipped');
  const lo = (list) => (list.length ? Math.min(...list.map((p) => p.y)) : null);
  const hi = (list) => (list.length ? Math.max(...list.map((p) => p.y)) : null);
  const bits = [];
  if (down.length) bits.push(`download ${mbps(lo(down))} to ${mbps(hi(down))} Mbps`);
  if (up.length) bits.push(`upload ${mbps(lo(up))} to ${mbps(hi(up))} Mbps`);
  const word = (n, what) => `${n} ${what}`;
  if (bad.length) bits.push(word(bad.length, 'failed'));
  if (skipped.length) bits.push(word(skipped.length, 'skipped'));
  return {
    xMin: from * 1000, xMax: edge * 1000, days: range,
    down, up, failed: bad, skipped,
    planDown: has(data.plan.down_mbps) && data.plan.down_mbps > 0 ? data.plan.down_mbps : null,
    planUp: has(data.plan.up_mbps) && data.plan.up_mbps > 0 ? data.plan.up_mbps : null,
    label: `Speed of the last ${range} days, ${tests.length} test${tests.length === 1 ? '' : 's'}: ${bits.join('; ')}.`,
    modes: RANGES.map((r) => ({ action: 'speedRange', arg: r.days, label: r.label, title: `Last ${r.days} days`, pressed: r.days === range }))
  };
}

// Why Run now cannot be pressed (null: it can). The server decides in the end; this only explains the common cases.
function runBlock(data) {
  if (!data.enabled) return 'The speed test is off. Turn it on in Settings first.';
  if (data.running) return 'A speed test is running.';
  if (data.monthly_cap_mb > 0 && data.month_used_mb >= data.monthly_cap_mb) return `This month's data limit is reached (${usageText(data)}).`;
  return null;
}

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
    chart: empty ? null : chartOf(d, now),
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
  document.getElementById('speedCard').dataset.status = vm.status;
}

// Polls again soon while a test runs, else once a minute.
function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, data && (data.running || starting) ? RUNNING_POLL_MS : POLL_MS);
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
  setInterval(render, REDRAW_MS);
  document.addEventListener('zonechange', render);
}

// The actions this card's views name (see dispatch.js).
registerActions('click', {
  speedAskRun: () => { confirming = true; note = null; render(); },
  speedRunCancel: () => { confirming = false; render(); },
  speedRunConfirm: () => { runNow(); },
  speedRange: (el) => { range = Number(el.dataset.arg) === 30 ? 30 : 7; render(); }
});
