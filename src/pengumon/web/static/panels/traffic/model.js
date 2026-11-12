// Internet use card, the model: the router's traffic statistics (GET /api/devices/{id}/traffic): the WAN's download and
// upload as a bar chart (per clock hour over 24 hours or 7 days, per day over 30 days), the devices that used the most, and
// the apps: the router's latest 24-hour window over 24 hours, the sum of the whole days (midnight to midnight) up to the
// last midnight over 7 and 30 days (the router counts apps only per 24 hours). Polled once a minute (the router publishes an hour a few minutes after it ends, and Pengumon reads
// it every hour or so). present() turns the answer into the view model every view of the card draws (view.js is the
// standard one; docs/themes.md, "Drawing a card"); the header's summary, the footer and the card's status are the frame,
// kept here.
//
// The honest gaps: an hour the router has not published yet is "not published yet" (null in the answer), never 0 bytes; an
// older hour that was never stored is "not stored" (Pengumon was not reading then).

import { createCard } from '../../js/cards.js';
import { registerActions } from '../../js/dispatch.js';
import { CLIENT_KINDS } from '../../js/device-text.js';
import { formatBytes, plural } from '../../js/format.js';
import { formatClock, localFormat } from '../../js/clock.js';
import standard from './view.js';
import { everyWhileVisible, whenVisible } from '../../js/timers.js';
import { has } from '../../js/values.js';

const POLL_MS = 60000;
const REDRAW_MS = 60000;
// `arg` names a range in its button and in the query; 30 days are whole days up to the last midnight, a bar per day.
export const RANGES = [
  { arg: '24', query: 'hours=24', hours: 24, label: '24 h', title: 'Last 24 hours' },
  { arg: '168', query: 'hours=168', hours: 168, label: '7 d', title: 'Last 7 days' },
  { arg: '30d', query: 'days=30', days: 30, label: '30 d', title: 'Last 30 days' }
];
const TOP = 10; // devices and apps asked for
const SHOWN = 8; // devices and apps listed

let device = null;
let data = null; // the last answer, null until it came
let failed = null; // why the last request failed, null while it worked
let range = '24';
let card = null;
let pollTimer = null;
let seq = 0;

// Seconds -> "5 min", "1 h", "2 d" (how often the router's figures are read).
function span(seconds) {
  if (seconds < 5400) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

const clock = (sec) => formatClock(sec * 1000);
// The range an answer is of (the range now chosen may be newer than the answer in hand), and "last 24 hours": what it covers.
const rangeOf = (d) => RANGES.find((r) => (d.days ? r.days === d.days : r.hours === d.hours)) || RANGES[0];
const covers = (d) => rangeOf(d).title.toLowerCase();
// A day as the answer names it ("2026-11-11"), in words: "Nov 11", or with the weekday "Wed, Nov 11". The answer cut the
// days on the zone set in Settings, so they are read as dates, never as instants in the browser's zone.
const DAY = new Intl.DateTimeFormat([], { timeZone: 'UTC', month: 'short', day: 'numeric' });
const WEEKDAY = new Intl.DateTimeFormat([], { timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric' });
export const dayWords = (day, weekday) => {
  const [y, m, dd] = day.split('-').map(Number);
  return (weekday ? WEEKDAY : DAY).format(Date.UTC(y, m - 1, dd));
};
const hourSpan = (hour) => `${clock(hour)}-${clock(hour + 3600)}`;
// "Mon 14:00" (the day is part of the label over 24 hours)
const stamp = (hour, days) => (days ? localFormat(hour * 1000, { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : clock(hour));

// The hours of a series as the chart draws them. `values`: [{hour, up, down}] oldest first, null when the hour is not stored.
// Hours after the newest stored one are "not published yet" (the router publishes an hour after it ends), earlier ones that
// were never stored "not stored". Plain data: the chart module colors it. `down`/`up` stay null for a gap, never 0.
export function hoursOf(rows, days) {
  let last = -1;
  rows.forEach((r, i) => { if (has(r.down)) last = i; });
  return {
    hours: rows.map((r) => r.hour),
    labels: rows.map((r) => stamp(r.hour, days)),
    titles: rows.map((r) => `${days ? `${localFormat(r.hour * 1000, { weekday: 'short' })} ` : ''}${hourSpan(r.hour)}`),
    down: rows.map((r) => (has(r.down) ? r.down : null)),
    up: rows.map((r) => (has(r.up) ? r.up : null)),
    gaps: rows.map((r, i) => (has(r.down) ? null : i > last ? 'unpublished' : 'nodata')),
    stored: last >= 0
  };
}

// The sentences under the chart about the gaps (text on the page, for a person who cannot read the hatching).
export function gapNotes(h) {
  const notes = [];
  const trailing = h.hours.filter((_, i) => h.gaps[i] === 'unpublished');
  if (trailing.length) {
    const from = trailing[0];
    const to = trailing[trailing.length - 1] + 3600;
    notes.push({ key: 'unpublished', text: trailing.length === 1
      ? `The newest hour (${hourSpan(from)}) is not published by the router yet.`
      : `The newest ${trailing.length} hours (${clock(from)}-${clock(to)}) are not published by the router yet.` });
  }
  const missing = h.gaps.filter((g) => g === 'nodata').length;
  if (missing) notes.push({ key: 'nodata', text: `${plural(missing, 'hour')} in this range ${missing === 1 ? 'was' : 'were'} not stored (Pengumon was not reading the router then).` });
  return notes;
}

// The days of a daily series as the chart draws them (the same shape as hoursOf's): a day without a stored hour is a gap
// ("not stored"); a day with some is drawn with what was stored, and the notes say how many such days there are.
export function daysOf(rows) {
  return {
    hours: rows.map((r) => r.start),
    labels: rows.map((r) => dayWords(r.day)),
    titles: rows.map((r) => `${dayWords(r.day, true)}${r.stored && r.stored < r.hours ? ` (${r.stored} of ${r.hours} hours stored)` : ''}`),
    down: rows.map((r) => (has(r.down) ? r.down : null)),
    up: rows.map((r) => (has(r.up) ? r.up : null)),
    gaps: rows.map((r) => (has(r.down) ? null : 'nodata')),
    stored: rows.some((r) => has(r.down))
  };
}

export function dayNotes(rows) {
  const notes = [];
  const missing = rows.filter((r) => !has(r.down)).length;
  const partial = rows.filter((r) => has(r.down) && r.stored < r.hours).length;
  if (missing) notes.push({ key: 'nodata', text: `${plural(missing, 'day')} in this range ${missing === 1 ? 'was' : 'were'} not stored (Pengumon was not reading the router then).` });
  if (partial) notes.push({ key: 'partial', text: `${plural(partial, 'day')} ${partial === 1 ? 'has' : 'have'} hours missing: ${partial === 1 ? 'its bar counts' : 'their bars count'} only the hours stored.` });
  return notes;
}

const modes = () => RANGES.map((r) => ({ action: 'trafficRange', arg: r.arg, label: r.label, title: r.title, pressed: r.arg === range }));

// The chart of 30 days: a bar per day.
function dailyChartOf(d) {
  const h = daysOf(d.daily);
  if (!h.stored) return null;
  const busiest = d.daily.reduce((best, r) => (has(r.down) && (!best || r.down > best.down) ? r : best), null);
  return {
    ...h,
    days: true,
    label: `Download and upload per day, ${covers(d)}: ${formatBytes(d.totals.down)} down, ${formatBytes(d.totals.up)} up.`,
    modes: modes(),
    notes: dayNotes(d.daily),
    busiest: busiest && busiest.down > 0 ? { text: `Busiest day ${dayWords(busiest.day, true)}: ${formatBytes(busiest.down)} down`, hour: busiest.start } : null
  };
}

// The chart's data for the wan series: {hours..., modes, label, notes}; null without a stored hour.
function chartOf(d) {
  if (Array.isArray(d.daily)) return dailyChartOf(d);
  const days = d.hours > 24;
  const h = hoursOf(d.wan, days);
  if (!h.stored) return null;
  const busiest = d.wan.reduce((best, r) => (has(r.down) && (!best || r.down > best.down) ? r : best), null);
  return {
    ...h,
    days,
    label: `Download and upload per hour, ${covers(d)}: ${formatBytes(d.totals.down)} down, ${formatBytes(d.totals.up)} up.`,
    modes: modes(),
    notes: gapNotes(h),
    busiest: busiest && busiest.down > 0 ? { text: `Busiest hour ${hourSpan(busiest.hour)}: ${formatBytes(busiest.down)} down`, hour: busiest.hour } : null
  };
}

// One device (or app) row. `max`: the biggest download in the list, for the bar; `all`: the whole download, for the share;
// `span`: "last 7 days" when the apps are the sum of the range's whole days, null when they are the router's latest 24 hours.
function rowOf(c, max, all, span) {
  const kind = CLIENT_KINDS[c.kind];
  return {
    key: c.key,
    name: c.label,
    nameTitle: c.mac ? `${c.label} (${c.mac})` : c.label,
    icon: kind ? kind[1] : 'host',
    sub: [kind ? kind[0] : null, c.vendor].filter(Boolean).join(' · '),
    app: c.traffic_hint ? `mostly ${c.traffic_hint}` : c.apps && c.apps.length ? `top app ${c.apps[0].app}` : '',
    appTitle: span
      ? (c.traffic_hint ? `The router counted at least 80 % of its traffic over the ${span} as this app` : `Its biggest app over the ${span}`)
      : (c.traffic_hint ? 'The router counted at least 80 % of its last 24 hours as this app' : 'Its biggest app in the router\'s latest 24-hour window'),
    down: formatBytes(c.down),
    up: formatBytes(c.up),
    pct: max > 0 ? Math.max(2, Math.round((c.down / max) * 100)) : 0,
    share: all > 0 ? `${c.down / all < 0.01 ? '<1' : Math.round((c.down / all) * 100)} % of all download` : '',
    open: { action: 'openTrafficClient', arg: device ? device.id : '', arg2: c.key, label: `Show the traffic of ${c.label}` }
  };
}

function clientsOf(d) {
  const rows = d.clients.slice(0, SHOWN);
  const max = rows.length ? Math.max(...rows.map((c) => c.down)) : 0;
  return {
    title: 'Top devices',
    sub: covers(d),
    more: d.clients.length > SHOWN ? `${d.clients.length - SHOWN} more with less traffic` : '',
    empty: rows.length ? '' : 'No device used the internet in this range.',
    rows: rows.map((c) => rowOf(c, max, d.totals.down, d.apps_days ? covers(d) : null))
  };
}

function appsOf(d, now) {
  const rows = d.apps.slice(0, SHOWN);
  const max = rows.length ? Math.max(...rows.map((a) => a.down)) : 0;
  const end = d.apps_window_end;
  // the day too when the window ended more than a day ago, or on another day than now
  const old = has(end) && now - end > 20 * 3600;
  // over days the apps say the range as the devices do; which days had figures is in the help (traffic.apps) and the answer
  const days = !!d.apps_days;
  return {
    title: 'Top apps',
    sub: days ? covers(d) : has(end) ? `apps over the 24 h to ${formatClock(end * 1000, old)}` : 'apps over the router\'s latest 24 h',
    more: '',
    empty: rows.length ? '' : days ? 'No whole day of app figures is stored for this range yet: Pengumon keeps one per day, read just after midnight.' : 'The router has no app figures yet.',
    rows: rows.map((a) => ({
      key: a.app, name: a.app, down: formatBytes(a.down), up: formatBytes(a.up),
      pct: max > 0 ? Math.max(2, Math.round((a.down / max) * 100)) : 0
    }))
  };
}

// The sentence for the footer: when the router's traffic was read and how often.
function readText(d) {
  if (!d) return 'waiting for the first reading';
  const every = has(d.interval_seconds) ? ` · every ${span(d.interval_seconds)}` : ' · switched off';
  return has(d.read_at) ? `read ${clock(d.read_at)}${every}` : `not read since Pengumon started${every}`;
}

// The view model: { state: 'loading' | 'error' | 'ready', error, summary, status, read, notice, totals, chart, clients, apps,
// empty }. (summary, status and read are the frame's, kept by render below.)
export function present(d, ui, now) {
  if (!d) {
    return ui.failed ? { state: 'error', error: `Could not load the internet use (${ui.failed}).`, summary: 'unavailable', status: 'unknown', read: 'waiting for the first reading' }
      : { state: 'loading', summary: 'loading...', status: 'unknown', read: 'waiting for the first reading' };
  }
  const chart = chartOf(d);
  const anything = !!chart || d.clients.length > 0 || d.apps.length > 0;
  const notice = d.available ? null : { level: 'warn', icon: 'warn', word: 'Not available', reason: d.reason || 'The router gives no traffic figures.' };
  let empty = null;
  if (!anything) empty = d.available ? 'No traffic has been read yet. The router publishes each hour a few minutes after it ends, so the first figures come within the hour.' : '';
  return {
    state: 'ready',
    summary: chart ? `${formatBytes(d.totals.down)} down · ${formatBytes(d.totals.up)} up` : d.available ? 'no data yet' : 'unavailable',
    status: d.available && chart ? 'good' : 'unknown',
    read: readText(d),
    stale: ui.failed ? `Could not refresh (${ui.failed}); this is the last answer.` : null,
    notice,
    empty,
    totals: chart ? {
      text: `${formatBytes(d.totals.down)} downloaded · ${formatBytes(d.totals.up)} uploaded`,
      range: Array.isArray(d.daily) && d.daily.length
        ? `${rangeOf(d).title}, ${dayWords(d.daily[0].day)} to ${dayWords(d.daily[d.daily.length - 1].day)} (whole days; today is in 24 h)`
        : `${covers(d).charAt(0).toUpperCase()}${covers(d).slice(1)}, up to ${formatClock(d.end * 1000, d.hours > 24)}`,
      busiest: chart.busiest
    } : null,
    chart,
    clients: anything ? clientsOf(d) : null,
    apps: anything ? appsOf(d, now) : null
  };
}

// Made-up answer for the parity test of the card's views: a day of traffic whose newest hour is not published, one hour with
// no data in the middle, three devices, three apps.
export function fixture() {
  const end = 1767225600; // a fixed moment, on the hour
  const wan = Array.from({ length: 24 }, (_, i) => {
    const hour = end - (24 - i) * 3600;
    if (i === 23) return { hour, up: null, down: null };
    if (i === 10) return { hour, up: null, down: null };
    return { hour, up: 200000000 + i * 1000000, down: i === 6 ? 75000000000 : 1500000000 + i * 40000000 };
  });
  const answer = {
    available: true, reason: null, read_at: end + 300, interval_seconds: 3600, hours: 24, start: end - 24 * 3600, end,
    window_end: end - 3600, stored_at: end + 300, wan, totals: { up: 5200000000, down: 118000000000 },
    clients: [
      { key: 'fx1', label: 'Example console', kind: 'game', vendor: 'Example Games', up: 4000000000, down: 100000000000, apps: [{ app: 'Example Play', up: 1, down: 1 }], traffic_hint: 'Example Play' },
      { key: 'fx2', label: 'Example phone', kind: 'phone', vendor: 'Example', up: 900000000, down: 12000000000, apps: [{ app: 'Example Video', up: 1, down: 1 }], traffic_hint: null },
      { key: 'fx3', label: 'Example plug', kind: null, vendor: null, up: 1000000, down: 5000000, apps: [], traffic_hint: null }
    ],
    apps: [{ app: 'Example Play', up: 4000000000, down: 100000000000 }, { app: 'Example Video', up: 900000000, down: 12000000000 }, { app: 'General', up: 1000000, down: 5000000 }],
    apps_window_end: end - 3600, privacy: 'full'
  };
  return present(answer, { failed: null }, end + 600);
}

// The range the card shows now (its arg: '24', '168' or '30d'): a device's dialog opens in it.
export const shownRange = () => range;

// Asks for the card's figures again now (a device was renamed in its dialog).
export function refreshTraffic() { if (device) refresh(); }

function render() {
  if (!card) return;
  const vm = present(data, { failed }, Date.now() / 1000);
  card.update(vm);
  const summary = document.getElementById('trafficSummary');
  if (summary) summary.textContent = vm.summary;
  const foot = document.getElementById('trafficFoot');
  if (foot) foot.textContent = vm.read;
  const frame = document.getElementById('trafficCard');
  if (frame.dataset.status !== vm.status) frame.dataset.status = vm.status; // (the same value written again would wake the tab-dot observer)
}

function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => whenVisible(refresh), POLL_MS);
}

async function refresh() {
  if (!device) return;
  const mine = ++seq;
  try {
    const chosen = RANGES.find((r) => r.arg === range) || RANGES[0];
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/traffic?${chosen.query}&top=${TOP}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (mine !== seq) return; // another range was chosen meanwhile
    data = body && Array.isArray(body.wan) ? body : null;
    failed = data ? null : 'unexpected answer';
  } catch (e) {
    if (mine !== seq) return;
    console.warn('Failed to load the internet use:', e);
    failed = e.message;
  }
  render();
  schedule();
}

export function initTraffic(dev) {
  const article = document.getElementById('trafficCard');
  if (!article || !dev) return;
  device = dev;
  card = createCard({ panel: 'traffic', root: article.querySelector('.panel-b'), standard, fixture });
  render();
  refresh();
  everyWhileVisible(render, REDRAW_MS);
  document.addEventListener('zonechange', render);
}

// The actions this card's views name (see dispatch.js).
registerActions('click', {
  trafficRange: (el) => {
    const arg = el.dataset.arg;
    if (!RANGES.some((r) => r.arg === arg) || arg === range) return;
    range = arg;
    render(); // the buttons show the choice at once; the figures follow with the answer
    refresh();
  }
});
