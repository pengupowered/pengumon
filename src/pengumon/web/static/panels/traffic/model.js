// Internet use card, the model: the router's traffic statistics (GET /api/devices/{id}/traffic): the WAN's download and
// upload as a bar chart (per clock hour over 24 hours or 7 days, per calendar day over 30 and 90 days, per week past that), the devices that used the
// most, and the apps. Every range is a rolling window that ends at the newest hour the router published (24, 168 and 720
// hours); the router counts apps only per 24 hours, so the apps are the sum of its 24-hour counts that lie inside the
// window, and what they do not account for is shown as "Not broken down by app". Polled once a minute (the router publishes
// an hour a few minutes after it ends, and Pengumon reads it every hour or so). present() turns the answer into the view model every view of the card draws (view.js is the
// standard one; docs/themes.md, "Drawing a card"); the header's summary, the footer and the card's status are the frame,
// kept here.
//
// The honest gaps: an hour the router has not published yet is "not published yet" (null in the answer), never 0 bytes; an
// older hour that was never stored is "not stored" (Pengumon was not reading then).

import { createCard } from '../../js/cards.js';
import { registerActions } from '../../js/dispatch.js';
import { CLIENT_KINDS } from '../../js/device-text.js';
import { formatBytes, plural } from '../../js/format.js';
import { formatClock, localFormat, nextMidnight } from '../../js/clock.js';
import { oldestStored } from '../../js/oldest.js';
import standard from './view.js';
import { everyWhileVisible, whenVisible } from '../../js/timers.js';
import { has } from '../../js/values.js';
import { allStoredWords, onRetentionChange, rangeOptions, retentionDays } from '../../js/ranges.js';

const POLL_MS = 60000;
const REDRAW_MS = 60000;
// The ranges on offer (js/ranges.js): all roll back from now. `arg` is the range's id ('24h', '7d', '30d', '90d', 'all'), `hours` what is
// asked of the router's traffic (GET /api/devices/{id}/traffic accepts up to 400 days). 24 h and 7 d are a bar per clock hour, 30 d
// and 90 d a bar per calendar day, and past 90 days a bar per week (a bar per day would be a hair wide).
const MAX_DAYS = 400;
export const rangeList = () => rangeOptions({ days: Math.min(retentionDays('traffic'), MAX_DAYS), min: '24h' }).map((o) => ({
  arg: o.id, hours: o.seconds / 3600, query: `hours=${o.seconds / 3600}`, label: o.short, title: o.words, all: o.all
}));
export const WEEKLY_FROM_HOURS = 90 * 24;
export const NOT_BROKEN_DOWN = 'Not broken down by app';
export const NOT_BROKEN_DOWN_WHY = 'The router counts apps per 24 hours; hours not covered by one of its counts in this range';
const TOP = 10; // devices and apps asked for
const SHOWN = 8; // devices and apps listed

let device = null;
let data = null; // the last answer, null until it came
let failed = null; // why the last request failed, null while it worked
let range = '24h';
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
const coversWords = (hours) => (hours <= 24 ? 'last 24 hours' : hours === 168 ? 'last 7 days' : `last ${Math.round(hours / 24)} days`);
// "All" says what it holds: "all stored, since Nov 8" when it starts at the first stored day, else "all stored, last 180 d".
const covers = (d) => (d.all ? allStoredWords(Math.round(d.hours / 24), d.since) : coversWords(d.hours));

// Asks for a range; for All, when the first stored day is later than the window's start by more than two days, asks again
// for the window from that day (the router's traffic is kept as hourly rows: a day with none is "not stored"), so the bars
// start at the data (days, or weeks only when the span drawn is past 90 days). `ask(hours)` -> the answer or null;
// `current()` is false when a newer choice was made. The answer says `all` and, when it starts late, `since` (epoch seconds).
export async function loadTraffic(chosen, ask, current) {
  // js/oldest.js says where the data starts: the first request is for the window from the first stored day, when that is later
  const stored = chosen.all ? await oldestStored('traffic') : null;
  const midnight = stored === null ? null : nextMidnight(stored * 1000) / 1000 - 86400;
  const now = Date.now() / 1000;
  const late = midnight !== null && midnight - (now - chosen.hours * 3600) > 2 * 86400 && current();
  const body = await ask(late ? Math.max(24, Math.ceil((now - midnight) / 3600)) : chosen.hours);
  if (!chosen.all || !body || !Array.isArray(body.daily)) return body;
  body.all = true;
  if (late) body.since = midnight;
  const first = body.daily.find((r) => has(r.down));
  if (!current() || !first || first.start - body.start <= 2 * 86400) return body;
  const again = await ask(Math.max(24, Math.ceil((body.end - first.start) / 3600)));
  if (!again) return body;
  again.all = true;
  again.since = first.start;
  return again;
}
// A day as the answer names it ("2026-11-11"), in words: "Nov 11", or with the weekday "Wed, Nov 11". The answer cut the
// days on the zone set in Settings, so they are read as dates, never as instants in the browser's zone. The hours a cut
// day holds are instants: "Fri 16:00-24:00" (clock times, as the rest of the card shows them).
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

// "16:00" and, for the midnight that ends a day, "24:00".
const HM23 = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
const clock23 = (sec) => localFormat(sec * 1000, HM23);
const clockEnd = (sec) => { const t = clock23(sec); return t === '00:00' ? '24:00' : t; };

// The days of a daily series as the chart draws them (the same shape as hoursOf's): a day without a stored hour is a gap
// ("not stored"); a day with some is drawn with what was stored, and the notes say how many such days there are. The range
// cuts the first and last day: such a cell is `partial`, drawn narrower in proportion to its hours (`shares`, 1 for a
// whole day, also one of 23 or 25 hours) and its title says which hours it holds.
export function daysOf(rows) {
  const unit = rows.length && rows[0].week ? 168 : 24;
  return {
    hours: rows.map((r) => r.start),
    labels: rows.map((r) => dayWords(r.day)),
    titles: rows.map((r) => {
      const held = r.week ? (r.partial ? ` (${Math.round(r.hours)} of 168 hours)` : '') : r.partial ? ` ${clock23(r.start)}-${clockEnd(r.end)}` : '';
      const missing = r.stored && r.stored < Math.floor(r.hours) ? ` (${r.stored} of ${Math.round(r.hours)} hours stored)` : '';
      return `${r.week ? `week of ${dayWords(r.day)}` : dayWords(r.day, true)}${held}${missing}`;
    }),
    down: rows.map((r) => (has(r.down) ? r.down : null)),
    up: rows.map((r) => (has(r.up) ? r.up : null)),
    gaps: rows.map((r) => (has(r.down) ? null : 'nodata')),
    shares: rows.map((r) => (r.partial ? Math.min(1, Math.max(0.05, r.hours / unit)) : 1)),
    stored: rows.some((r) => has(r.down))
  };
}

// The daily rows of a range past 90 days as weeks (Monday to Sunday, the first and last cut by the range): the sums of their
// days, `partial` when the range cuts the week (it holds fewer than 166 of its 168 hours at an end of the range).
export function weeklyOf(rows) {
  if (!rows || !rows.length) return rows;
  const weeks = [];
  rows.forEach((r, i) => {
    const [y, m, d] = r.day.split('-').map(Number);
    const dow = (new Date(Date.UTC(y, m - 1, d, 12)).getUTCDay() + 6) % 7; // Monday = 0
    const monday = new Date(Date.UTC(y, m - 1, d - dow, 12)).toISOString().slice(0, 10);
    let w = weeks[weeks.length - 1];
    if (!w || w.day !== monday) { w = { week: true, day: monday, start: r.start, end: r.end, hours: 0, stored: 0, down: null, up: null, partial: false, first: i === 0 }; weeks.push(w); }
    w.end = r.end;
    w.hours += r.hours;
    w.stored += r.stored || 0;
    if (has(r.down)) w.down = (w.down || 0) + r.down;
    if (has(r.up)) w.up = (w.up || 0) + r.up;
    w.last = i === rows.length - 1;
  });
  weeks.forEach((w) => { w.partial = (w.first || w.last) && w.hours < 166; });
  return weeks;
}
// The rows a daily answer is drawn from: days, or weeks past 90 days.
export const dailyRows = (d) => (d.hours > WEEKLY_FROM_HOURS ? weeklyOf(d.daily) : d.daily);

export function dayNotes(rows) {
  const notes = [];
  const unit = rows.length && rows[0].week ? 'week' : 'day';
  const missing = rows.filter((r) => !has(r.down)).length;
  const partial = rows.filter((r) => has(r.down) && r.stored < Math.floor(r.hours)).length;
  const cut = rows.filter((r) => r.partial).length;
  if (cut) notes.push({ key: 'cut', text: `The range begins and ends inside a ${unit}: those bars are narrower and count only the hours it holds (hover for which).` });
  if (missing) notes.push({ key: 'nodata', text: `${plural(missing, unit)} in this range ${missing === 1 ? 'was' : 'were'} not stored (Pengumon was not reading the router then).` });
  if (partial) notes.push({ key: 'partial', text: `${plural(partial, unit)} ${partial === 1 ? 'has' : 'have'} hours missing: ${partial === 1 ? 'its bar counts' : 'their bars count'} only the hours stored.` });
  return notes;
}

const modes = () => rangeList().map((r) => ({ action: 'trafficRange', arg: r.arg, label: r.title, title: r.title, pressed: r.arg === range }));

// The chart of 30 and 90 days: a bar per calendar day; past 90 days a bar per week (the first and last cut by the range).
function dailyChartOf(d) {
  const rows = dailyRows(d);
  const h = daysOf(rows);
  if (!h.stored) return null;
  const week = d.hours > WEEKLY_FROM_HOURS;
  const busiest = rows.reduce((best, r) => (has(r.down) && (!best || r.down > best.down) ? r : best), null);
  return {
    ...h,
    days: true,
    label: `Download and upload per ${week ? 'week' : 'day'}, ${covers(d)}: ${formatBytes(d.totals.down)} down, ${formatBytes(d.totals.up)} up.`,
    modes: modes(),
    notes: dayNotes(rows),
    busiest: busiest && busiest.down > 0 ? { text: week ? `Busiest week, from ${dayWords(busiest.day, true)}: ${formatBytes(busiest.down)} down` : `Busiest day ${dayWords(busiest.day, true)}: ${formatBytes(busiest.down)} down`, hour: busiest.start } : null
  };
}

// The chart's data for the wan series: {hours..., modes, label, notes}; null without a stored hour.
function chartOf(d) {
  if (!d.wan.length && Array.isArray(d.daily)) return dailyChartOf(d);
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
// `span`: "last 7 days", the range the apps were counted in.
const unbroken = (c) => !!c.apps_unattributed && (c.apps_unattributed.down > 0 || c.apps_unattributed.up > 0);

function rowOf(c, max, all, span) {
  const kind = CLIENT_KINDS[c.kind];
  return {
    key: c.key,
    name: c.label,
    nameTitle: c.mac ? `${c.label} (${c.mac})` : c.label,
    icon: kind ? kind[1] : 'host',
    sub: [kind ? kind[0] : null, c.vendor].filter(Boolean).join(' · '),
    app: c.traffic_hint ? `mostly ${c.traffic_hint}` : c.apps && c.apps.length ? `top app ${c.apps[0].app}` : unbroken(c) ? 'apps not broken down' : '',
    appTitle: c.apps && c.apps.length
      ? (c.traffic_hint ? `The router counted at least 80 % of its traffic as this app, over the hours it counted apps for in the ${span}` : `Its biggest app over the hours the router counted apps for in the ${span}`)
      : NOT_BROKEN_DOWN_WHY,
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
    rows: rows.map((c) => rowOf(c, max, d.totals.down, covers(d)))
  };
}

// The apps list: the apps the router counted inside the range and, last, what it did not break down (muted).
function appsOf(d, now) {
  const rows = d.apps.slice(0, SHOWN);
  const rest = d.apps_unattributed || { up: 0, down: 0 };
  const left = rest.down > 0 || rest.up > 0;
  const max = Math.max(0, ...rows.map((a) => a.down), left ? rest.down : 0);
  const end = d.apps_window_end;
  const counted = d.apps_covered ? d.apps_covered.hours : 0;
  // the day too when the newest count ended more than a day ago, or on another day than now
  const old = has(end) && now - end > 20 * 3600;
  const list = rows.map((a) => ({
    key: a.app, name: a.app, down: formatBytes(a.down), up: formatBytes(a.up),
    pct: max > 0 ? Math.max(2, Math.round((a.down / max) * 100)) : 0
  }));
  if (left) {
    list.push({ key: '__rest', name: NOT_BROKEN_DOWN, title: NOT_BROKEN_DOWN_WHY, muted: true, down: formatBytes(rest.down), up: formatBytes(rest.up),
      pct: max > 0 ? Math.max(2, Math.round((rest.down / max) * 100)) : 0 });
  }
  return {
    title: 'Top apps',
    sub: counted ? `${counted === d.hours ? covers(d) : `counted over ${counted} of ${d.hours} h`}, to ${formatClock(end * 1000, old)}` : 'the router has no 24 h count in this range',
    more: '',
    empty: list.length ? '' : 'The router has no app figures yet.',
    rows: list
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
      range: `${covers(d).charAt(0).toUpperCase()}${covers(d).slice(1)}, from ${formatClock(d.start * 1000, true)} to ${formatClock(d.end * 1000, true)}`,
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
    apps_unattributed: { up: 299000000, down: 995000000 }, apps_covered: { hours: 24, windows: [end - 3600] },
    apps_window_end: end - 3600, privacy: 'full'
  };
  return present(answer, { failed: null }, end + 600);
}

// The range the card shows now (its arg: a range id of js/ranges.js): a device's dialog opens in it.
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
    const list = rangeList();
    const chosen = list.find((r) => r.arg === range) || list[0];
    range = chosen.arg;
    const body = await loadTraffic(chosen, async (hours) => {
      const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/traffic?hours=${hours}&top=${TOP}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }, () => mine === seq);
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
  onRetentionChange(() => { render(); refresh(); });
}

// The actions this card's views name (see dispatch.js).
registerActions('change', {
  trafficRange: (el) => {
    const arg = el.value;
    if (!rangeList().some((r) => r.arg === arg) || arg === range) return;
    range = arg;
    render(); // the control shows the choice at once; the figures follow with the answer
    refresh();
  }
});
