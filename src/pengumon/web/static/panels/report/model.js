// Reliability report, the model (/api/report): availability per day and hop, downtime, causes and the worst hours.
// present() makes the view model every view of the card draws (view.js is the standard one; docs/themes.md, "Drawing a
// card"); the range buttons and the footer (worst hour) are the frame, kept here.
//
// The range is a rolling window ending now, so its first and last day are cut: the report marks them (`partial`, with the
// `hours` each holds in the range) and a bar's tooltip says so.
//
// The headline is the internet as seen through the public targets (the median of their availability; the last hop
// when there is no public target); days are bars of what that figure lost (a clean day has no bar, so a bad day stands
// out; bars of 99.9x % all looked full), causes the share of downtime per cause, and a table gives every hop. Nothing is
// colored by a threshold here: bars are neutral, the numbers say it.

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { createCard } from '../../js/cards.js';
import { percentText } from '../../js/format.js';
import { median } from '../../js/values.js';
import { targetList } from '../../js/targets.js';
import { zoneParams } from '../../js/clock.js';
import { quietKey, quietTargets, shortLabels } from '../../js/path.js';
import { CAUSE_LONG } from '../../js/roles.js';
import { stripBars, stripStyle } from '../../js/spike-strip.js';
import standard from './view.js';
import { everyWhileVisible } from '../../js/timers.js';
import { fillRangeSelect, onRetentionChange, rangeOptions, retentionDays } from '../../js/ranges.js';

let card = null;
let root = null;
let shown = null; // the last report, drawn again when the hops that ignore ping change
let quiet = new Map(); // target id -> its neutral words (path.js quietTargets)
let seq = 0;

// A hop that ignores ping (path.js quietTargets, from the live `targets` topic) is listed with those words instead of an
// availability that is only its unanswered pings, and never stands in for the internet figure; so is an IPv6 target
// while this host has no IPv6, with its own words.
function followQuietHops() {
  return bus.subscribe('targets', (targets) => {
    const next = quietTargets(targets);
    if (quietKey(next) === quietKey(quiet)) return;
    quiet = next;
    if (shown && card) draw(shown);
  });
}

// The cause categories (the server's `cause`) with what each means: js/path-api.js CAUSES. An unknown one is shown as it is.
const causeText = (cause) => CAUSE_LONG[cause] || String(cause || 'unknown').replace(/_/g, ' ');

const fmtPct = (v) => percentText(v, 3);
// minutes as the report says them: "41 min", "1h 05m"
const minutesText = (m) => (m >= 60 ? `${Math.floor(m / 60)}h ${String(Math.round(m % 60)).padStart(2, '0')}m` : `${Math.round(m)} min`);
// The period's outages (incidents that belong together, GET /api/report incidents.outages) next to its incidents; null when the server sends none.
function outagesText(o) {
  if (!o || !(o.count > 0)) return o ? 'No outages' : null;
  return `${o.count} outage${o.count === 1 ? '' : 's'}, down ${minutesText(o.down_minutes)} in all, longest ${minutesText(o.longest_minutes)}`
    + (o.maintenance > 0 ? `, ${o.maintenance} likely maintenance` : '');
}
const weekText = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
const hour = (h) => `${String(h).padStart(2, '0')}:00`;
// "14 h" or "11 h 53 min": the part of a cut day the range holds
const WEEK_FROM_DAYS = 31; // past this many days the bars are weeks (a bar per day would be a hair wide in this card: 90 bars were under 1 px)
const MAX_DAYS = 400; // what GET /api/report answers
// The days (dates 'YYYY-MM-DD' on the report's clock) as Monday-start weeks: [{ start, idx: [day indexes] }].
function weeksOf(days) {
  const weeks = [];
  days.forEach((d, i) => {
    const dow = (new Date(`${d}T12:00:00Z`).getUTCDay() + 6) % 7; // Monday = 0
    if (!weeks.length || dow === 0) weeks.push({ start: d, idx: [] });
    weeks[weeks.length - 1].idx.push(i);
  });
  return weeks;
}
const holdText = (h) => { const m = Math.round(h * 60); return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`; };

// The view model of a report: { state: 'ready', hero, downtime, incidents, lost, causes, excluded, hops, foot, summary }.
// `ctx`: { targets: [{id, category}], labels, quiet } (what the page knows of the targets).
export function present(report, ctx) {
  const inc = report.incidents;
  const days = report.availability.days;
  const hops = report.availability.hops;
  const meta = new Map(ctx.targets.map((t) => [t.id, t]));
  let pub = hops.filter((h) => (meta.get(h.target_id) || {}).category === 'public');
  const heard = hops.filter((h) => !ctx.quiet.has(h.target_id));
  if (!pub.length) pub = heard.length ? [heard[heard.length - 1]] : [];
  const avail = median(pub.map((h) => h.availability_pct));
  const perDay = days.map((_, i) => median(pub.map((h) => h.per_day[i])));
  // Past 31 days a bar is a week (Monday to Sunday, the first and last cut by the range): the hours each day holds in the range
  // weigh its availability, and a cut week's bar is as narrow as the hours it holds.
  const weekly = report.days > WEEK_FROM_DAYS;
  const hoursOf = (i) => (report.availability.partial && report.availability.partial[i] ? report.availability.hours[i] : 24);
  const units = weekly ? weeksOf(days).map((w) => {
    const have = w.idx.filter((i) => perDay[i] !== null && perDay[i] !== undefined);
    const hrs = w.idx.reduce((a, i) => a + hoursOf(i), 0);
    const held = have.reduce((a, i) => a + hoursOf(i), 0);
    const v = held > 0 ? have.reduce((a, i) => a + perDay[i] * hoursOf(i), 0) / held : null;
    return { id: w.start, name: `week of ${weekText(w.start)}`, v, hours: hrs, cut: (w.idx.includes(0) || w.idx.includes(days.length - 1)) && hrs < 166 };
  }) : days.map((d, i) => ({ id: d, name: d, v: perDay[i], hours: hoursOf(i), cut: !!(report.availability.partial && report.availability.partial[i]), day: true }));
  const lostPct = units.map((u) => (u.v === null || u.v === undefined ? null : Math.max(0, 100 - u.v)));
  const peak = Math.max(0, ...lostPct.filter((v) => v !== null));
  const peakDay = lostPct.indexOf(peak);
  const minutes = inc.minutes || 0;
  const downtime = minutes >= 60 ? `${Math.floor(minutes / 60)}h ${String(Math.round(minutes % 60)).padStart(2, '0')}m` : `${minutes.toFixed(minutes < 10 ? 1 : 0)} min`;
  const totalMin = inc.by_cause.reduce((a, c) => a + c.minutes, 0) || 1;
  const dayName = (d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: 'short' });
  const note = peak > 0 ? `Worst ${weekly ? 'week' : 'day'}: ${weekly ? units[peakDay].name : `${dayName(days[peakDay])} ${days[peakDay]}`}, ${fmtPct(peak)} lost` : `No probe lost on any ${weekly ? 'week' : 'day'}`;
  const worstHour = report.worst_hours[0];
  // Brief latency spikes (not incidents): a count per cell of the bars above (a day, or a week past 31 days), from availability.latency_spikes
  const perUnit = report.availability.latency_spikes;
  const spikeCount = report.latency_spikes && Number.isFinite(report.latency_spikes.count) ? report.latency_spikes.count : null;
  const spikes = Array.isArray(perUnit) && perUnit.length === days.length && spikeCount !== null ? (() => {
    const counts = weekly ? weeksOf(days).map((w) => w.idx.reduce((a, i) => a + perUnit[i], 0)) : perUnit;
    const cells = units.map((u, i) => ({ id: u.id, name: u.name, count: counts[i], hours: u.hours, cut: u.cut }));
    const unit = weekly ? 'week' : 'day';
    const avg = spikeCount / Math.max(1, report.days);
    const avgText = `avg ${avg === 0 ? '0' : avg.toFixed(1)}/day`;
    return { title: `Latency spikes per ${unit}`, avg: avgText, columns: Math.max(1, units.length), style: stripStyle(cells), bars: stripBars(cells, unit),
      aria: `Latency spikes per ${unit}, ${spikeCount} in ${report.days} days, ${avgText}` };
  })() : null;
  const name = (h) => ({ text: ctx.labels[h.target_id] || h.name, title: h.name });
  return {
    state: 'ready',
    hero: {
      label: `Availability, last ${report.days} days`,
      value: avail === null ? '–' : percentText(avail).slice(0, -1),
      unit: '%',
      sub: pub.length > 1 ? `Internet: median of ${pub.length} public servers` : (pub[0] ? ctx.labels[pub[0].target_id] || pub[0].name : 'no probe data yet'),
      subTitle: pub.map((h) => h.name).join(', ')
    },
    downtime: { label: 'Downtime', value: downtime },
    incidents: { label: `Incidents, ${report.days} d`, value: String(inc.count), sub: outagesText(inc.outages) },
    lost: {
      title: weekly ? 'Lost per week' : 'Lost per day',
      columns: Math.max(1, units.length),
      // the grid: a bar's width follows the hours it holds (a cut day or week is narrower); a closer gap for many bars
      style: `grid-template-columns: ${units.some((u) => u.hours !== units[0].hours) ? units.map((u) => `minmax(0, ${Math.round(u.hours * 1000) / 1000}fr)`).join(' ') : `repeat(${Math.max(1, units.length)}, minmax(0, 1fr))`}; gap: ${units.length > 60 ? 1 : units.length > 31 ? 2 : units.length > 14 ? 4 : 6}px`,
      aria: `Share of probes lost per ${weekly ? 'week' : 'day'}. ${note}`,
      note,
      bars: units.map((u, i) => {
        const v = u.v;
        const none = v === null || v === undefined;
        const lost = lostPct[i];
        const part = u.cut ? ` · only ${holdText(u.hours)} of this ${weekly ? 'week' : 'day'} are in the range` : '';
        return { id: u.id, tip: `${u.name} · ${none ? 'no data' : `${fmtPct(v)} answered, ${fmtPct(lost)} lost`}${part}`, nodata: none, partial: u.cut,
          height: lost === null || peak <= 0 ? 0 : Math.max(4, (lost / peak) * 100), letter: !weekly && units.length <= 10 ? dayName(u.id).slice(0, 1) : '' };
      })
    },
    spikes,
    causes: {
      title: 'Downtime by cause',
      empty: 'No incidents.',
      items: inc.by_cause.slice(0, 5).map((c) => {
        const share = Math.round((c.minutes / totalMin) * 100);
        return { id: c.cause, title: c.cause, label: causeText(c.cause), share, shareText: `${share}%` };
      })
    },
    excluded: inc.excluded_expected > 0 ? `${inc.excluded_expected} expected busy-line event${inc.excluded_expected === 1 ? '' : 's'} not counted (latency under heavy load is not a fault).` : null,
    hops: {
      summary: 'Every hop',
      columns: ['Hop', 'Availability', 'Worst day'],
      empty: 'No probe data yet.',
      rows: hops.map((h) => {
        if (ctx.quiet.has(h.target_id)) {
          const q = ctx.quiet.get(h.target_id);
          return { id: h.target_id, name: name(h), quiet: true, avail: q.word, availTitle: q.why, worst: '–' };
        }
        let worst = null;
        h.per_day.forEach((v, i) => { if (v !== null && (worst === null || v < worst.v)) worst = { v, day: days[i] }; });
        return { id: h.target_id, name: name(h), quiet: false, avail: fmtPct(h.availability_pct), availTitle: '', worst: worst ? `${fmtPct(worst.v)} on ${worst.day}` : '–' };
      })
    },
    foot: worstHour ? `Worst hour: ${hour(worstHour.hour)} (${worstHour.loss_pct}% loss)` : 'No packet loss recorded.',
    summary: `Last ${report.days} days: ${inc.count} incidents, ${Math.round(minutes)} min`
  };
}

// Made-up report for the parity test of the card's views: three days, a quiet hop, two causes, an expected event.
export function fixture() {
  const days = ['2026-01-01', '2026-01-02', '2026-01-03'];
  const report = {
    days: 3,
    availability: { days, latency_spikes: [2, 5, 0], hops: [
      { target_id: 'fx_gateway', name: 'Example gateway', availability_pct: 99.9, per_day: [100, 99.8, 99.9] },
      { target_id: 'fx_quiet', name: 'Example hop 2', availability_pct: 12.5, per_day: [12, 13, 12] },
      { target_id: 'fx_public', name: 'Example public DNS', availability_pct: 99.95, per_day: [100, 99.9, null] }
    ] },
    incidents: { count: 2, minutes: 75, excluded_expected: 1, outages: { count: 1, down_minutes: 41, longest_minutes: 25, maintenance: 1 }, by_cause: [{ cause: 'gateway', minutes: 60 }, { cause: 'line', minutes: 15 }] },
    latency_spikes: { count: 7, expected: 0, by_stage: [{ stage: 'isp', count: 7 }], incident_after_seconds: 120 },
    worst_hours: [{ hour: 20, loss_pct: 1.5 }]
  };
  return present(report, {
    targets: [{ id: 'fx_gateway', category: 'gateway' }, { id: 'fx_quiet', category: 'isp' }, { id: 'fx_public', category: 'public' }],
    labels: { fx_gateway: 'Gateway', fx_quiet: 'ISP hop 2', fx_public: 'Internet' },
    quiet: new Map([['fx_quiet', { word: 'Ignores ping', why: 'Does not answer ping, but the hops after it do' }]])
  });
}

function draw({ report }) {
  const list = targetList();
  const vm = present(report, { targets: list, labels: shortLabels(list), quiet });
  card.update(vm);
  const foot = document.getElementById('reportFoot');
  if (foot) foot.textContent = vm.foot;
  root.dataset.summary = vm.summary;
}

// Reliability report (/api/report)
// The range control: 7, 30, 90 days and All (the probes' retention, at most what the route answers).
const options = () => rangeOptions({ days: Math.min(retentionDays('probes'), MAX_DAYS), min: '7d' });
let rangeId = '7d';
const daysOf = (id) => Math.round(((options().find((o) => o.id === id) || options()[0]).seconds) / 86400);
let range = 7; // the days asked for
function markRange() {
  const list = options();
  if (!list.some((o) => o.id === rangeId)) rangeId = '7d'; // (a retention that no longer keeps the range)
  range = daysOf(rangeId);
  const host = document.getElementById('reportRangeButtons');
  if (host) fillRangeSelect(host, { options: list, value: rangeId, action: 'loadReport', label: 'Report range' });
}
async function loadReport(days, refresh = false) {
  if (!card) return;
  range = days;
  const mine = ++seq;
  if (!refresh) card.update({ state: 'loading' }); // (a refresh of what is shown keeps showing it)
  try {
    // Day and hour buckets are cut server-side, in the Settings zone, else this browser's.
    const res = await fetch(`/api/report?days=${days}&${zoneParams()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const report = await res.json();
    if (mine !== seq) return;
    shown = { report };
    draw(shown);
  } catch (err) {
    if (mine === seq && !(refresh && shown)) card.update({ state: 'error', error: `Error generating report: ${err.message}` });
  }
}

export function initReport(section) {
  root = section.querySelector('.panel-b');
  card = createCard({ panel: 'report', root, standard, fixture });
  markRange();
  onRetentionChange(() => { markRange(); loadReport(range); });
  loadReport(range);
  followQuietHops();
  // the headline, downtime and incident count follow the line: read again when an incident opens, ends or changes, when
  // the zone changes (the buckets are cut in it), and now and then
  const again = () => loadReport(range, true);
  let version;
  bus.subscribe('incidents_version', (v) => {
    const first = version === undefined;
    if (v !== version) { version = v; if (!first) again(); }
  });
  document.addEventListener('zonechange', again);
  everyWhileVisible(again, 300000);
}

// The actions this card's markup names (see dispatch.js).
registerActions('change', {
  loadReport: (el) => {
    if (!options().some((o) => o.id === el.value) || el.value === rangeId) return;
    rangeId = el.value;
    markRange();
    loadReport(range);
  }
});
