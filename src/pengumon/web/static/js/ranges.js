// The one range control of every chart: a drop-down (a native <select>, so it works by keyboard, on a phone and with a
// screen reader) with the same words and order on every card. This file owns the list of ranges, which of them a chart
// offers (from the finest step of its data and how long that kind of data is kept) and the control's markup.
//
//   rangeOptions({ days, min: '1h' })  -> [{ id, seconds, short, words, all }]
//   rangeSelect({ options, value, action, label })  -> a <span class="range-pick"> with the <select> (data-change=action)
//   fillRangeSelect(host, options, value, ...)  -> the same into an existing element; options changed only when they differ
//   retentionDays(kind)  -> the days that kind of data is kept (0: forever), from the settings the page already holds
//
// The ranges are rolling windows ending now. "All" is the whole retention of the kind of data ("All · 180 d"), and a
// chart shows what is stored when that is less.

import { formatClock, localFormat, zoneOffsetMs } from './clock.js';
import { oldestStored } from './oldest.js';
import { settingsValues, val } from './settings-values.js';

const DAY = 86400;
export const DEFAULT_RETENTION_DAYS = 180;
// "Keep forever" (incidents: 0): All then reaches the oldest stored row; the longest range a route answers is 3650 days.
export const FOREVER_DAYS = 3650;

// Live is the latency card's 60 s feed (no window). Ids are what the cards and settings (display.default_latency_range) call them.
export const RANGES = [
  { id: 'live', seconds: 0, short: 'Live', words: 'Live' },
  { id: '15m', seconds: 900, short: '15 m', words: 'Last 15 minutes' },
  { id: '1h', seconds: 3600, short: '1 h', words: 'Last hour' },
  { id: '24h', seconds: DAY, short: '24 h', words: 'Last 24 hours' },
  { id: '7d', seconds: 7 * DAY, short: '7 d', words: 'Last 7 days' },
  { id: '30d', seconds: 30 * DAY, short: '30 d', words: 'Last 30 days' },
  { id: '90d', seconds: 90 * DAY, short: '90 d', words: 'Last 90 days' }
];
const BY_ID = new Map(RANGES.map((r) => [r.id, r]));
export const rangeById = (id) => BY_ID.get(id) || null;

// The setting that says how long each kind of data is kept (settings values, GET /api/settings/values).
export const RETENTION_KEYS = {
  probes: 'database.latency_retention_days', // latency, target history, uptime strips, "When it breaks", report
  router: 'database.router_retention_days', // router history, client counts
  mesh: 'database.mesh_retention_days',
  line: 'database.line_retention_days', // the cable line's channels
  traffic: 'database.traffic_retention_days', // Internet use
  speedtest: 'database.speedtest_retention_days',
  incidents: 'database.incident_retention_days' // 0: forever
};

// Days a kind of data is kept: the setting, else the default (180). 0 is "forever" (incidents only).
export function retentionDays(kind, values = settingsValues()) {
  const v = Number(val(values, RETENTION_KEYS[kind], DEFAULT_RETENTION_DAYS));
  return Number.isFinite(v) && v >= 0 ? v : DEFAULT_RETENTION_DAYS;
}

// "All · 180 d", "All · 1 y" (a whole number of years), "All" when the data is kept forever.
export function allWords(days) {
  if (!days) return 'All';
  return days % 365 === 0 ? `All · ${days / 365} y` : `All · ${days} d`;
}

// The ranges a chart offers. `days`: how long its kind of data is kept (0: forever); `min`: the shortest range its data
// can fill (a range finer than the data is left out: speed tests come daily, so their chart starts at 7 d); `max`: the
// longest fixed range (the AI pack stops at '30d' and has no All); `live` / `quarter`: the latency views' Live and 15 m.
// 90 d is left out when the kind keeps less than 90 days, and All when it equals a range already offered.
export function rangeOptions({ days = DEFAULT_RETENTION_DAYS, min = '1h', max = '90d', live = false, quarter = false, all = true } = {}) {
  const lo = (BY_ID.get(min) || BY_ID.get('1h')).seconds;
  const hi = (BY_ID.get(max) || BY_ID.get('90d')).seconds;
  const kept = days > 0 ? days * DAY : FOREVER_DAYS * DAY;
  const out = RANGES.filter((r) => {
    if (r.id === 'live') return live;
    if (r.id === '15m') return quarter;
    return r.seconds >= lo && r.seconds <= hi && r.seconds <= kept;
  }).map((r) => ({ ...r, all: false }));
  const longest = out.reduce((m, r) => Math.max(m, r.seconds), 0);
  // (forever, or a retention past the longest fixed range)
  if (all && max === '90d' && kept > longest) {
    const words = allWords(days);
    out.push({ id: 'all', seconds: kept, short: words, words, all: true });
  }
  return out;
}

// Runs `fn` when the settings the page holds were read again (a Save changed a retention): a control offers the new set.
export function onRetentionChange(fn) {
  document.addEventListener('settingsvalues', fn);
}

// Seconds per bucket of a history chart over `seconds`: a width from the steps below that gives at most `points` points
// (about 400-800 a line). Past 30 days the widths are whole multiples of 15 minutes, the stored tier.
const NICE_BUCKETS = [900, 1800, 3600, 5400, 7200, 10800, 14400, 21600, 43200, 86400];
export function longBucket(seconds, points = 800) {
  const want = seconds / points;
  const nice = NICE_BUCKETS.find((b) => b >= want);
  return nice || Math.ceil(want / 86400) * 86400;
}

// The first of the months inside [min, max] (epoch ms, midnights on the page's clock), thinned to every 1st, 2nd, 3rd, 6th or
// 12th month so there are at most `most` of them: the ticks of a long axis, labeled with monthLabel.
export function monthTicks(min, max, most = 7) {
  const wall = (y, m) => { const w = Date.UTC(y, m, 1); return w - zoneOffsetMs(w - zoneOffsetMs(w)); };
  const from = new Date(min + zoneOffsetMs(min));
  const all = [];
  for (let y = from.getUTCFullYear(), m = from.getUTCMonth(); all.length < 600; m++) {
    const t = wall(y + Math.floor(m / 12), ((m % 12) + 12) % 12);
    if (t > max) break;
    if (t >= min) all.push({ t, index: y * 12 + m });
  }
  const step = [1, 2, 3, 6, 12].find((k) => all.filter((x) => x.index % k === 0).length <= most) || 12;
  return all.filter((x) => x.index % step === 0).map((x) => x.t);
}
const MONTH_YEAR = { month: 'short', year: 'numeric' };
export const monthLabel = (ms) => localFormat(ms, MONTH_YEAR);

// A bucket width in words: "15 min", "6 h", "1 d".
export function stepWords(seconds) {
  if (seconds >= 86400 && seconds % 86400 === 0) return `${seconds / 86400} d`;
  if (seconds >= 3600) return `${Math.round((seconds / 3600) * 10) / 10} h`;
  if (seconds >= 60) return `${Math.round(seconds / 60)} min`;
  return `${seconds} s`;
}

// The option of an id (or of a window in seconds), or the nearest one a chart offers: a remembered choice may not be
// offered any longer (a shorter retention was set).
export function pickRange(options, id, fallback) {
  return options.find((o) => o.id === id) || options.find((o) => o.id === fallback) || options[0] || null;
}

const sig = (options, extra) => JSON.stringify([options.map((o) => [o.id, o.words]), extra || null]);

function build(select, options, value, extra) {
  select.replaceChildren(...options.map((o) => {
    const opt = document.createElement('option');
    opt.value = o.id;
    opt.textContent = o.words;
    return opt;
  }));
  if (extra) { // a state that is no range of the list (the latency chart showing an incident's window)
    const opt = document.createElement('option');
    opt.value = extra.id;
    opt.textContent = extra.words;
    opt.disabled = true;
    select.appendChild(opt);
  }
  select.dataset.sig = sig(options, extra);
  if (value !== undefined) select.value = value;
}

// A new control: <span class="range-pick"><label class="sr-only">Range</label><select class="input range-select">.
// `action`: the data-change name the card registered; `extra`: {id, words} of a state shown when `value` is it.
export function rangeSelect({ options, value, action, label = 'Range', id, extra }) {
  const host = document.createElement('span');
  host.className = 'range-pick';
  const sel = document.createElement('select');
  sel.className = 'input range-select';
  if (id) sel.id = id;
  sel.setAttribute('aria-label', label);
  if (action) sel.setAttribute('data-change', action);
  host.appendChild(sel);
  build(sel, options, value, value === (extra && extra.id) ? extra : null);
  return host;
}

// Puts options and value into the control inside `host` (made there on the first call). The options are only rebuilt
// when they differ, so an open list is not closed by a redraw that changed nothing.
export function fillRangeSelect(host, { options, value, action, label, id, extra }) {
  let sel = host.querySelector('select.range-select');
  if (!sel) {
    host.replaceChildren(rangeSelect({ options, value, action, label, id, extra }));
    return host.querySelector('select');
  }
  const showExtra = extra && value === extra.id ? extra : null;
  if (sel.dataset.sig !== sig(options, showExtra)) build(sel, options, value, showExtra);
  else if (sel.value !== value) sel.value = value;
  return sel;
}

// "All" shows what is stored: when the oldest stored row is later than the window's start by more than a bucket or two, the
// chart starts there. `kind` (probes, router, mesh, line, speedtest: js/oldest.js, GET /api/oldest) says where the data
// starts, so this asks once, for the window from the oldest row (floored to a whole bucket of that span's width, so the
// buckets suit the span drawn: 400-800 points, not the retention's). Without `kind`, or when the server does not say, it asks
// for the whole window and, when the answer starts late, again from its oldest row. A device younger than the oldest row of its
// kind (a second router) still gets that second request.
//   ask(start, end, bucket) -> an answer (null when the request failed); oldest(answer) -> epoch seconds of its oldest row, or null
//   bucketOf(seconds) -> the bucket width of a span (omit when the server picks it: then `widthOf(answer)` says what it chose)
//   current() -> false when a newer choice was made: the second request is then not sent (callers check their own `seq` after)
// Returns { answer, start, bucket, since }; `since` is the start (epoch seconds) when drawn from the oldest row, else null.
export async function loadFromOldest({ all, kind, start, end, bucketOf, widthOf, ask, oldest, current = () => true }) {
  let bucket = bucketOf ? bucketOf(end - start) : undefined;
  let since = null;
  const stored = all && kind ? await oldestStored(kind) : null;
  if (stored !== null && current() && stored - start > (bucket || 900) * 2) {
    const next = bucketOf ? bucketOf(end - stored) : undefined;
    const unit = next || 60;
    start = Math.floor(stored / unit) * unit;
    bucket = next;
    since = start;
  }
  const answer = await ask(start, end, bucket);
  const whole = { answer, start, bucket, since };
  if (!all || !answer || !current()) return whole;
  const first = oldest(answer);
  const width = (widthOf && widthOf(answer)) || bucket || 60;
  // (a start the server's oldest time gave is right to within a quarter hour: one target or device may begin a little later than the rest)
  if (first === null || first === undefined || first - start <= Math.max(width * 2, since === null ? 0 : 900)) return whole;
  const next = bucketOf ? bucketOf(end - first) : undefined;
  const unit = next || 60;
  const from = Math.floor(first / unit) * unit;
  const again = await ask(from, end, next);
  if (!again) return whole;
  return { answer: again, start: from, bucket: next, since: from };
}

// The label of a chart's range: "all stored, last 180 d" or, when it starts at the oldest stored row, "all stored, since Nov 8".
export function allStoredWords(days, since) {
  if (since) return `all stored, since ${localFormat(since * 1000, { month: 'short', day: 'numeric', ...(Date.now() / 1000 - since > 300 * DAY ? { year: 'numeric' } : {}) })}`;
  return `all stored, ${days ? `last ${days} d` : 'every day kept'}`;
}

// A time on an axis, by the span the chart draws (not the range's name): the clock to a day, date and clock to a week,
// dates to 90 days, months beyond.
// Months only past 90 days; a day of slack, since a chart that draws its points (not its window) spans a bucket more or less than its range.
export const monthsPast = (seconds) => seconds > 91 * DAY;
export function axisLabel(ms, seconds) {
  if (monthsPast(seconds)) return monthLabel(ms);
  if (seconds > 7 * DAY) return localFormat(ms, { month: 'short', day: 'numeric' });
  return formatClock(ms, seconds > DAY);
}

// The ticks of a category axis (one label per point) when neighbouring points share a label (a date or a month over many
// points): the first point of each label, thinned evenly to at most `most`, so no label shows twice in a row. null when no
// two neighbours share one (the axis keeps its own ticks). Chart.js blanks labels before it skips ticks, so a callback that
// blanks a repeat cannot do this; use it from the scale's afterBuildTicks.
export function distinctTickIndexes(labels, most = 8) {
  if (!Array.isArray(labels) || !labels.some((l, i) => i > 0 && l === labels[i - 1])) return null;
  const starts = [];
  labels.forEach((l, i) => { if (i === 0 || l !== labels[i - 1]) starts.push(i); });
  const n = Math.max(2, most);
  const pick = starts.length <= n ? starts : Array.from({ length: n }, (_, k) => starts[Math.round((k * (starts.length - 1)) / (n - 1))]);
  return pick.map((value) => ({ value }));
}

// A tick callback for a numeric time axis that leaves out a label equal to the one before it (two ticks in one day).
export const distinctTicks = (label) => (v, i, ticks) => {
  const text = label(v);
  return i > 0 && ticks[i - 1] && label(ticks[i - 1].value) === text ? '' : text;
};
