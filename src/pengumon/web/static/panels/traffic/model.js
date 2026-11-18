// Internet use card, the model: the router's traffic statistics (GET /api/devices/{id}/traffic): the range's download and
// upload at the top of the card (the big figures are always the range chosen), the WAN's download and upload as a line (a point
// per clock hour up to 90 days, per calendar day past that, drawn as the Speed card's history), the hour (or day) picked on it
// in a strip under the chart with its figures (owner's choice, 2026-11-17: "range big, hour as a strip"), the devices that
// used the most over the range (over the picked hour while one is picked: GET /api/devices/{id}/traffic/span, asked once per
// pick and kept; Whole range, or the picked point clicked again, goes back), and the apps (none for a picked hour: the router
// counts them per 24 hours). Every range is a rolling window
// that ends at the newest hour the router published (1, 24, 168 and 720 hours, ...); the router counts apps only per 24
// hours, so the apps are the sum of its 24-hour counts that lie inside the window, and what they do not account for is shown
// as "Not broken down by app". Last hour is the newest hour alone: no line, its devices are the list. Polled once a minute
// (the router publishes an hour a few minutes after it ends, and Pengumon reads it every hour or so). present() turns the
// answer into the view model every view of the card draws (view.js is the standard one; docs/themes.md, "Drawing a card");
// the header's summary, the footer and the card's status are the frame, kept here.
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
// The ranges on offer (js/ranges.js): all roll back from now. `arg` is the range's id ('1h', '24h', '7d', '30d', '90d', 'all'),
// `hours` what is asked of the router's traffic (GET /api/devices/{id}/traffic accepts up to 400 days). Up to 90 days a point per
// clock hour (asked with `hourly`), past that a point per calendar day. The card offers Last hour (`hour`); the device dialog
// starts at 24 hours.
const MAX_DAYS = 400;
export const HOURLY_MAX_HOURS = 90 * 24;
export const rangeList = ({ hour = false } = {}) => rangeOptions({ days: Math.min(retentionDays('traffic'), MAX_DAYS), min: hour ? '1h' : '24h' }).map((o) => ({
  arg: o.id, hours: o.seconds / 3600, query: `hours=${o.seconds / 3600}`, label: o.short, title: o.words, all: o.all
}));
const cardRanges = () => rangeList({ hour: true });
export const NOT_BROKEN_DOWN = 'Not broken down by app';
export const NOT_BROKEN_DOWN_WHY = 'The router counts apps per 24 hours; hours not covered by one of its counts in this range';
const TOP = 10; // devices and apps asked for
const SHOWN = 8; // devices and apps listed
const SPANS_KEPT = 200; // picked hours (and days) whose figures are kept in the page

let device = null;
let data = null; // the last answer, null until it came
let failed = null; // why the last request failed, null while it worked
let range = '24h';
// The hour or day picked on the chart (the strip and the lists are then its own); null: none. An hour is {start, end}; a day
// is {day} ("2026-11-17"): the first and last day of a range past 90 days are cut by the rolling window, so their start or end
// moves every hour while the day stays the one picked (and its span, asked for by start and end, is asked for again then).
let picked = null;
const spans = new Map(); // `${start}-${end}` -> the span answer, 'loading' or 'failed'
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
const coversWords = (hours) => (hours <= 1 ? 'last hour' : hours <= 24 ? 'last 24 hours' : hours === 168 ? 'last 7 days' : `last ${Math.round(hours / 24)} days`);
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

// The hours of a series as the chart draws them (chart.js). `rows`: [{hour, up, down}] oldest first, null when the hour is not
// stored. Hours after the newest stored one are "not published yet" (the router publishes an hour after it ends), earlier ones
// that were never stored "not stored". Plain data: the chart module colors it. `down`/`up` stay null for a gap, never 0.
export function hoursOf(rows, days) {
  let last = -1;
  rows.forEach((r, i) => { if (has(r.down)) last = i; });
  return {
    unit: 'hour',
    hours: rows.map((r) => r.hour),
    ends: rows.map((r) => r.hour + 3600),
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
// cuts the first and last day: such a day is `partial`, its point counts only the hours it holds and its title says which.
export function daysOf(rows) {
  return {
    unit: 'day',
    hours: rows.map((r) => r.start),
    ends: rows.map((r) => r.end),
    titles: rows.map((r) => {
      const held = r.partial ? ` ${clock23(r.start)}-${clockEnd(r.end)}` : '';
      const missing = r.stored && r.stored < Math.floor(r.hours) ? ` (${r.stored} of ${Math.round(r.hours)} hours stored)` : '';
      return `${dayWords(r.day, true)}${held}${missing}`;
    }),
    down: rows.map((r) => (has(r.down) ? r.down : null)),
    up: rows.map((r) => (has(r.up) ? r.up : null)),
    gaps: rows.map((r) => (has(r.down) ? null : 'nodata')),
    stored: rows.some((r) => has(r.down))
  };
}

export function dayNotes(rows) {
  const notes = [];
  const missing = rows.filter((r) => !has(r.down)).length;
  const partial = rows.filter((r) => has(r.down) && r.stored < Math.floor(r.hours)).length;
  const cut = rows.filter((r) => r.partial).length;
  if (cut) notes.push({ key: 'cut', text: 'The range begins and ends inside a day: those points count only the hours it holds (hover for which).' });
  if (missing) notes.push({ key: 'nodata', text: `${plural(missing, 'day')} in this range ${missing === 1 ? 'was' : 'were'} not stored (Pengumon was not reading the router then).` });
  if (partial) notes.push({ key: 'partial', text: `${plural(partial, 'day')} ${partial === 1 ? 'has' : 'have'} hours missing: ${partial === 1 ? 'its point counts' : 'their points count'} only the hours stored.` });
  return notes;
}

const modes = () => cardRanges().map((r) => ({ action: 'trafficRange', arg: r.arg, label: r.title, title: r.title, pressed: r.arg === range }));

// The index of the picked hour or day in a series (`h`: hoursOf/daysOf); -1 when none is picked or it is not in the series.
function pickedIndex(h, days) {
  if (!picked) return -1;
  if (picked.day) return days ? days.indexOf(picked.day) : -1;
  if (days) return -1;
  const i = h.hours.indexOf(picked.start);
  return i >= 0 && h.ends[i] === picked.end ? i : -1;
}

// The chart's data: {hours, ends, titles, down, up, gaps, unit, picked, chosen, newest, modes, label, notes, single}; null without
// a stored hour. `picked` is the index of the hour or day picked on the chart (its point marked), null when none is; `chosen`
// whether one is. A point per hour (`wan`, up to 90 days), else per calendar day (`daily`). `single`: Last hour, one point and no line.
// The series of an answer, made once per answer (90 days are 2160 points with three clock texts each, and a click asks for
// the chart's data more than once); a change of the page's zone makes it again (the texts are clock times).
let series = { of: null, h: null };
function seriesOf(d, daily) {
  if (series.of !== d) series = { of: d, h: daily ? daysOf(d.daily) : hoursOf(d.wan, d.hours > 24) };
  return series.h;
}

function chartOf(d) {
  const daily = !d.wan.length && Array.isArray(d.daily);
  const h = seriesOf(d, daily);
  if (!h.stored) return null;
  let newest = -1;
  h.down.forEach((v, i) => { if (v !== null) newest = i; });
  const unit = daily ? 'day' : 'hour';
  const at = d.hours <= 1 ? -1 : pickedIndex(h, daily ? d.daily.map((r) => r.day) : null);
  return {
    ...h,
    picked: at >= 0 ? at : null,
    chosen: at >= 0,
    days: daily ? d.daily.map((r) => r.day) : null,
    newest,
    single: d.hours <= 1,
    label: `Download and upload per ${unit}, ${covers(d)}: ${formatBytes(d.totals.down)} down, ${formatBytes(d.totals.up)} up.`,
    modes: modes(),
    notes: daily ? dayNotes(d.daily) : gapNotes(h)
  };
}

// "7.4 Mbps on average": bytes over seconds.
const average = (bytes, seconds) => `${(bytes * 8 / seconds / 1e6).toFixed(1)} Mbps on average`;
// A size as the big figure shows it: {num: '1.66', unit: 'GB'} (formatBytes' words split).
function bigFigure(bytes) {
  const [num, unit] = formatBytes(bytes).split(' ');
  return { num, unit: unit || '' };
}
const WHEN_HOUR = { weekday: 'short', month: 'short', day: 'numeric' };

// When the next figures come: the router publishes an hour a few minutes after it ends, and Pengumon reads it then (its traffic
// rounds start 5 minutes after the hour when read hourly); read less often, the next reading. Past that time it is late: said so.
function nextText(d, now) {
  const hourly = has(d.interval_seconds) && d.interval_seconds <= 3600;
  const next = has(d.window_end) && hourly ? d.window_end + 3600 + 300 : has(d.read_at) && has(d.interval_seconds) ? d.read_at + d.interval_seconds : null;
  return next ? `the next ${hourly ? 'hour' : 'reading'} ${next > now - 600 ? 'comes' : 'was due'} about ${clock(next)}` : '';
}

// The top of the card: the range chosen, its download and upload and their average speed over it. Always the range: a picked
// hour is the strip under the chart.
function topOf(d) {
  const from = d.since || d.start;
  const seconds = Math.max(3600, d.end - from);
  const figure = (bytes) => ({ ...bigFigure(bytes), avg: average(bytes, seconds) });
  const words = covers(d);
  return {
    when: `${words.charAt(0).toUpperCase()}${words.slice(1)}`,
    sub: `${formatClock(from * 1000, true)} to ${formatClock(d.end * 1000, true)}`,
    down: figure(d.totals.down),
    up: figure(d.totals.up)
  };
}

// The strip under the chart while an hour or day is picked on it: when, its download and upload, their average speed, and the
// steps ‹ › and Whole range. The lists below are then its own. `c`: chartOf's, with an index picked.
function stripOf(d, c) {
  const i = c.picked;
  const start = c.hours[i];
  const end = c.ends[i];
  const day = c.unit === 'day';
  const words = day ? `${dayWords(d.daily[i].day, true)}${d.daily[i].partial ? ` ${clock23(start)}-${clockEnd(end)}` : ''}`
    : `${localFormat(start * 1000, WHEN_HOUR)}, ${hourSpan(start)}`;
  const gap = c.gaps[i] === 'unpublished' ? 'not published yet' : 'not stored';
  const stored = has(c.down[i]);
  return {
    key: `${start}-${end}`,
    start, end,
    when: words,
    down: stored ? `${formatBytes(c.down[i])} down` : gap,
    up: stored && has(c.up[i]) ? `${formatBytes(c.up[i])} up` : '',
    avg: stored ? average(c.down[i], end - start) : '',
    steps: { prev: i > 0, next: i < c.newest, unit: c.unit }
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

// The devices of the picked hour or day (its span answer: `got`), as the range's are listed: no app line (the router counts apps
// per 24 hours). `c`: chartOf's.
function pickedClientsOf(c, got) {
  const i = c.picked;
  const words = c.unit === 'day' ? `this day, ${dayWords(c.days[i], true)}` : `this hour, ${localFormat(c.hours[i] * 1000, { weekday: 'short' })} ${hourSpan(c.hours[i])}`;
  if (!got || typeof got !== 'object') {
    return { title: 'Top devices', sub: words, more: '', empty: got === 'failed' ? `Could not load this ${c.unit}'s devices.` : 'loading…', rows: [] };
  }
  const rows = got.clients.slice(0, SHOWN);
  const max = rows.length ? Math.max(...rows.map((x) => x.down)) : 0;
  return {
    title: 'Top devices',
    sub: words,
    more: got.clients.length > SHOWN ? `${got.clients.length - SHOWN} more with less traffic` : '',
    empty: rows.length ? '' : `No device used the internet in this ${c.unit}.`,
    rows: rows.map((x) => rowOf({ ...x, apps: [], traffic_hint: null, apps_unattributed: null }, max, got.down || 0, words))
  };
}

// The apps of a picked hour or day: none (the router counts apps over 24 hours, and Pengumon keeps them per range and per day).
const pickedAppsOf = (c) => ({
  title: 'Top apps', sub: 'counted per 24 hours only', more: '', rows: [],
  empty: `The router counts apps over 24 hours, not per ${c.unit}, so there are none for one ${c.unit}. Whole range shows the range's apps.`
});

function clientsOf(d) {
  const rows = d.clients.slice(0, SHOWN);
  const max = rows.length ? Math.max(...rows.map((c) => c.down)) : 0;
  return {
    title: 'Top devices',
    sub: covers(d),
    more: d.clients.length > SHOWN ? `${d.clients.length - SHOWN} more with less traffic` : '',
    empty: rows.length ? '' : 'No device used the internet in this range.',
    // over the last hour the router has no app count: no app line under each device
    rows: rows.map((c) => rowOf(d.hours <= 1 ? { ...c, apps: [], traffic_hint: null, apps_unattributed: null } : c, max, d.totals.down, covers(d)))
  };
}

// The apps list: the apps the router counted inside the range and, last, what it did not break down (muted).
function appsOf(d, now) {
  if (d.hours <= 1) return { title: 'Top apps', sub: 'counted per 24 hours only', more: '', empty: 'The router counts apps over 24 hours, not per hour: choose 24 hours or longer to see them.', rows: [] };
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

// The sentence for the footer: when the router's traffic was read, how often, and when the next figures come.
function readText(d, now) {
  if (!d) return 'waiting for the first reading';
  const every = has(d.interval_seconds) ? ` · every ${span(d.interval_seconds)}` : ' · switched off';
  const next = has(d.interval_seconds) && now ? nextText(d, now) : '';
  return has(d.read_at) ? `read ${clock(d.read_at)}${every}${next ? ` · ${next}` : ''}` : `not read since Pengumon started${every}`;
}

// The view model: { state: 'loading' | 'error' | 'ready', error, summary, status, read, notice, totals, pick, chart, clients,
// apps, empty }: `totals` the top of the card (the range), `pick` the strip of the picked hour (null while none is). (summary,
// status and read are the frame's, kept by render below.)
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
    read: readText(d, now),
    stale: ui.failed ? `Could not refresh (${ui.failed}); this is the last answer.` : null,
    notice,
    empty,
    totals: chart ? topOf(d) : null,
    pick: chart && chart.chosen && !chart.single ? stripOf(d, chart) : null,
    chart,
    clients: !anything ? null : chart && chart.chosen ? pickedClientsOf(chart, (ui.spans || spans).get(`${chart.hours[chart.picked]}-${chart.ends[chart.picked]}`)) : clientsOf(d),
    apps: !anything ? null : chart && chart.chosen ? pickedAppsOf(chart) : appsOf(d, now)
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
  const newest = end - 2 * 3600; // the newest hour with a figure (the last is not published)
  const known = new Map([[`${newest}-${newest + 3600}`, { start: newest, end: newest + 3600, stored: 1, up: 222000000, down: 2420000000, clients: [
    { key: 'fx2', label: 'Example phone', up: 100000000, down: 2000000000 }, { key: 'fx3', label: 'Example plug', up: 1000000, down: 5000000 }] }]]);
  return present(answer, { failed: null, spans: known }, end + 600);
}

// The range the card shows now (its arg: a range id of js/ranges.js): a device's dialog opens in it.
export const shownRange = () => range;

// Asks for the card's figures again now (a device was renamed in its dialog).
export function refreshTraffic() { if (device) refresh(); }

function render() {
  if (!card) return;
  const vm = present(data, { failed }, Date.now() / 1000);
  card.update(vm);
  if (vm.pick && !spans.has(vm.pick.key)) loadSpan(vm.pick.start, vm.pick.end);
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
    const list = cardRanges();
    const chosen = list.find((r) => r.arg === range) || list[0];
    range = chosen.arg;
    const body = await loadTraffic(chosen, async (hours) => {
      const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/traffic?hours=${hours}&top=${TOP}${hours <= HOURLY_MAX_HOURS ? '&hourly=true' : ''}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }, () => mine === seq);
    if (mine !== seq) return; // another range was chosen meanwhile
    data = body && Array.isArray(body.wan) ? body : null;
    failed = data ? null : 'unexpected answer';
    if (picked && data) { const c = chartOf(data); if (!c || !c.chosen) picked = null; } // a pick the range no longer holds
    for (const [key, got] of spans) if (got === 'failed') spans.delete(key); // asked for again (a 429, a network blip)
  } catch (e) {
    if (mine !== seq) return;
    console.warn('Failed to load the internet use:', e);
    failed = e.message;
  }
  render();
  schedule();
}

// The figures and top devices of a picked hour or day (GET /api/devices/{id}/traffic/span), asked once and kept: a past hour does
// not change. Drawn again when the answer comes.
async function loadSpan(start, end) {
  const key = `${start}-${end}`;
  spans.set(key, 'loading');
  if (spans.size > SPANS_KEPT) spans.delete(spans.keys().next().value);
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/traffic/span?start=${start}&end=${end}&top=${TOP}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    spans.set(key, body && Array.isArray(body.clients) ? body : 'failed');
  } catch (e) {
    console.warn('Failed to load the picked hour of the internet use:', e);
    spans.set(key, 'failed');
  }
  render();
}

// Picks the hour or day at `index` of the chart now drawn: the strip and the lists become its own.
function pickAt(index) {
  const c = data ? chartOf(data) : null;
  if (!c || c.single) return;
  const i = Math.max(0, Math.min(c.newest, index));
  picked = c.unit === 'day' ? { day: c.days[i] } : { start: c.hours[i], end: c.ends[i] };
  const key = `${c.hours[i]}-${c.ends[i]}`;
  if (spans.get(key) === 'failed') spans.delete(key); // picked again: asked for again
  render();
}

export function initTraffic(dev) {
  const article = document.getElementById('trafficCard');
  if (!article || !dev) return;
  device = dev;
  card = createCard({ panel: 'traffic', root: article.querySelector('.panel-b'), standard, fixture });
  render();
  refresh();
  everyWhileVisible(render, REDRAW_MS);
  document.addEventListener('zonechange', () => { series = { of: null, h: null }; render(); });
  onRetentionChange(() => { render(); refresh(); });
}

// The actions this card's views name (see dispatch.js).
registerActions('change', {
  trafficRange: (el) => {
    const arg = el.value;
    if (!cardRanges().some((r) => r.arg === arg) || arg === range) return;
    range = arg;
    render(); // the control shows the choice at once; the figures follow with the answer
    refresh();
  }
});
registerActions('click', {
  // a click on the chart: the view put the start and end of the nearest point on the canvas (data-arg, data-arg2)
  trafficPick: (el) => {
    const c = data ? chartOf(data) : null;
    const start = Number(el.dataset.arg);
    if (!c || !el.dataset.arg) return;
    const i = c.hours.indexOf(start);
    if (i < 0 || c.ends[i] !== Number(el.dataset.arg2)) return;
    if (i === c.picked) { picked = null; render(); } else pickAt(i); // the picked point again: back to the whole range
  },
  trafficStep: (el) => {
    const c = data ? chartOf(data) : null;
    if (c && c.chosen) pickAt(c.picked + (el.dataset.arg === 'next' ? 1 : -1));
  },
  trafficWholeRange: () => { picked = null; render(); }
});
