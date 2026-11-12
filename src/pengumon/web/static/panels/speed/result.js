// Speed card, the pure part of the model: an answer of GET /api/speedtests as the words and figures the card shows (the last
// result against the plan, the delay grade, the history chart's data, when the next test is due, the data use, why Run
// now is off). Functions of the answer and the moment; nothing here reads the page or the module state of model.js.

import { localFormat } from '../../js/clock.js';
import { STATUS } from '../../js/dom.js';
import { has } from '../../js/values.js';

export const PROVIDERS = { cloudflare: 'Cloudflare', mlab: 'M-Lab', librespeed: 'LibreSpeed' };
const RANGES = [{ days: 7, label: '7 d' }, { days: 30, label: '30 d' }];
// A result's status as the card says it (never color alone: icon and word).
const RESULT = {
  ok: { level: 'good', word: 'Done' },
  partial: { level: 'warn', word: 'Partial' },
  failed: { level: 'serious', word: 'Failed' },
  skipped: { level: 'unknown', word: 'Skipped' }
};
const GRADE_LEVEL = { 'A+': 'good', A: 'good', B: 'warn', C: 'warn', D: 'serious', F: 'crit' };

const fmtInt = (n) => Math.round(n).toLocaleString('en-US');

// 212.4 -> "212", 8.46 -> "8.5": three figures are plenty for a speed
function mbps(v) {
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

function agoText(ts, now) {
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

export function usageText(data) {
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
export function lastResult(data, now) {
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
export function problem(data, now) {
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
export function chartOf(data, now, range) {
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
export function runBlock(data) {
  if (!data.enabled) return 'The speed test is off. Turn it on in Settings first.';
  if (data.running) return 'A speed test is running.';
  if (data.monthly_cap_mb > 0 && data.month_used_mb >= data.monthly_cap_mb) return `This month's data limit is reached (${usageText(data)}).`;
  return null;
}

