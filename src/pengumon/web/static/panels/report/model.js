// Reliability report, the model (/api/report): availability per day and hop, downtime, causes and the worst hours.
// present() makes the view model every view of the card draws (view.js is the standard one; docs/themes.md, "Drawing a
// card"); the range buttons and the footer (worst hour) are the frame, kept here.
//
// The headline is the internet as seen through the public targets (the median of their availability; the last hop
// when there is no public target); days are bars of what that figure lost (a clean day has no bar, so a bad day stands
// out; bars of 99.9x % all looked full), causes the share of downtime per cause, and a table gives every hop. Nothing is
// colored by a threshold here: bars are neutral, the numbers say it.

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { createCard } from '../../js/cards.js';
import { median, percentText, targetList, zoneParams } from '../../js/util.js';
import { quietKey, quietTargets, shortLabels } from '../../js/path.js';
import { CAUSE_LONG } from '../../js/path-api.js';
import standard from './view.js';

let card = null;
let root = null;
let shown = null; // the last report, drawn again when the hops that ignore ping change
let quiet = new Map(); // target id -> its neutral words (path.js quietTargets)
let seq = 0;

// A hop that ignores ping (path.js quietTargets, from the live `targets` topic) is listed with those words instead of an
// availability that is only its unanswered pings, and never stands in for the internet figure; so is an IPv6 target
// while this host has no IPv6, with its own words.
export function followQuietHops() {
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
const hour = (h) => `${String(h).padStart(2, '0')}:00`;

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
  const lostPct = perDay.map((v) => (v === null || v === undefined ? null : Math.max(0, 100 - v)));
  const peak = Math.max(0, ...lostPct.filter((v) => v !== null));
  const peakDay = lostPct.indexOf(peak);
  const minutes = inc.minutes || 0;
  const downtime = minutes >= 60 ? `${Math.floor(minutes / 60)}h ${String(Math.round(minutes % 60)).padStart(2, '0')}m` : `${minutes.toFixed(minutes < 10 ? 1 : 0)} min`;
  const totalMin = inc.by_cause.reduce((a, c) => a + c.minutes, 0) || 1;
  const dayName = (d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: 'short' });
  const note = peak > 0 ? `Worst: ${dayName(days[peakDay])} ${days[peakDay]}, ${fmtPct(peak)} lost` : 'No probe lost on any day';
  const worstHour = report.worst_hours[0];
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
    incidents: { label: `Incidents, ${report.days} d`, value: String(inc.count) },
    lost: {
      title: 'Lost per day',
      columns: Math.max(1, days.length),
      aria: `Share of probes lost per day. ${note}`,
      note,
      bars: days.map((d, i) => {
        const v = perDay[i];
        const none = v === null || v === undefined;
        const lost = lostPct[i];
        return { id: d, tip: `${d} · ${none ? 'no data' : `${fmtPct(v)} answered, ${fmtPct(lost)} lost`}`, nodata: none,
          height: lost === null || peak <= 0 ? 0 : Math.max(4, (lost / peak) * 100), letter: days.length > 10 ? '' : dayName(d).slice(0, 1) };
      })
    },
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
    availability: { days, hops: [
      { target_id: 'fx_gateway', name: 'Example gateway', availability_pct: 99.9, per_day: [100, 99.8, 99.9] },
      { target_id: 'fx_quiet', name: 'Example hop 2', availability_pct: 12.5, per_day: [12, 13, 12] },
      { target_id: 'fx_public', name: 'Example public DNS', availability_pct: 99.95, per_day: [100, 99.9, null] }
    ] },
    incidents: { count: 2, minutes: 75, excluded_expected: 1, by_cause: [{ cause: 'gateway', minutes: 60 }, { cause: 'line', minutes: 15 }] },
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
export async function loadReport(days) {
  if (!card) return;
  const mine = ++seq;
  document.querySelectorAll('#reportRangeButtons button').forEach((b) => {
    b.classList.toggle('active', b.dataset.arg === String(days));
    b.setAttribute('aria-pressed', String(b.dataset.arg === String(days)));
  });
  card.update({ state: 'loading' });
  try {
    // Day and hour buckets are cut server-side, in the Settings zone, else this browser's.
    const res = await fetch(`/api/report?days=${days}&${zoneParams()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const report = await res.json();
    if (mine !== seq) return;
    shown = { report };
    draw(shown);
  } catch (err) {
    if (mine === seq) card.update({ state: 'error', error: `Error generating report: ${err.message}` });
  }
}

export function initReport(section) {
  root = section.querySelector('.panel-b');
  card = createCard({ panel: 'report', root, standard, fixture });
  loadReport(7);
  followQuietHops();
}

// The actions this card's markup names (see dispatch.js).
registerActions('click', {
  loadReport: (el) => loadReport(Number(el.dataset.arg))
});
