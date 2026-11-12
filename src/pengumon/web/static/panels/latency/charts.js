// The latency panel's model: the data both charts draw and the view model built from it (view.js is the standard view,
// drawing a latency chart and a thin loss lane under it; docs/themes.md, "Drawing a card"). Two ways to draw the latency:
//   path (default): three series in path order (router, line, internet; path.js), the internet's p10-p90 band when the
//     server sends it, each series named at its line end. The legend says which targets each series is made of, and
//     the tooltip gives every member's own value.
//   each target: one thin line per pinged target (ISP hops included, DNS lookups left out: they time a lookup, not a
//     round trip), in the targets' own colors (the ones Hop details uses).
// Both get the WAN download as a faint background on the right axis and the incident windows shaded.
//
// The data: `view` = { times, series: {id: [ms]}, byTarget: {targetId: [ms]}, lo, hi, loss, lossBy, wan }. The live
// feed appends a point per message (updateLiveCharts); the history views replace it (history.js). Every change ends in
// renderCharts(), which hands the card a new view model (present()).

import { maxLivePoints, state } from '../../js/state.js';
import { formatClock } from '../../js/clock.js';
import { formatMs } from '../../js/format.js';
import { getTargetInfo, targetList } from '../../js/targets.js';
import { SERIES, combine, memberNames, seriesMembers, shortLabels } from '../../js/path.js';
import { createCard } from '../../js/cards.js';
import { primaryDevice } from '../../js/devices.js';
import { settingsValues, val } from '../../js/settings-values.js';
import standard from './view.js';
import { everyWhileVisible } from '../../js/timers.js';
import { has } from '../../js/values.js';

export const view = { times: [], series: {}, byTarget: {}, lo: [], hi: [], loss: [], lossBy: [], wan: [] };

// Which lines the chart draws: 'path' or 'targets'; kept per browser (a convenience, so failures are ignored).
const VIEW_KEY = 'pengumon.latency.view';
let lineMode = 'path';
try { if (localStorage.getItem(VIEW_KEY) === 'targets') lineMode = 'targets'; } catch { /* storage blocked */ }

// The pinged targets in path order (DNS lookups are not round trips and stay out of the chart).
export const pingTargets = (all = targetList()) => all.filter((t) => t.enabled !== false && t.category !== 'dns');
let noPings = false; // the live targets say this host cannot send pings (probing_unavailable)
let incidents = []; // [{start, end, level}] for the shaded windows
let card = null;

const clock = (ts, seconds) => formatClock(ts * 1000, false, !!seconds); // js/clock.js formatters are made once; the chart relabels up to 240 points on every message

export function resetView() {
  view.times = [];
  view.series = {};
  view.byTarget = {};
  view.lo = [];
  view.hi = [];
  view.loss = [];
  view.lossBy = [];
  view.wan = [];
}

const fmtMs = (v) => formatMs(v, { unit: true, empty: 'no reply', tenths: true });

// The lines to draw: the path series (with the internet's band) or one per pinged target. A color is a token (the
// card's own colors apply) or a target's own color.
function lines(data, mode, all) {
  const out = [];
  if (mode === 'targets') {
    const labels = shortLabels(all);
    pingTargets(all).forEach((t) => {
      if (!data.byTarget[t.id]) return;
      out.push({ id: `t:${t.id}`, kind: 'target', target: t.id, label: labels[t.id] || t.name, color: { value: t.color }, data: data.byTarget[t.id] });
    });
    return { lines: out, band: null };
  }
  let band = null;
  SERIES.forEach((s) => {
    if (!data.series[s.id]) return;
    if (s.id === 'path_internet' && data.lo.some(has)) band = { lo: data.lo, hi: data.hi, color: { token: s.color } };
    out.push({ id: s.id, kind: 'path', label: s.name, color: { token: s.color }, dash: s.dash, data: data.series[s.id] });
  });
  return { lines: out, band };
}

// The tooltip of a path series: its value, then each member's own value at that moment in `data` (a median of three says
// little until you see the three).
function memberLines(all, data, seriesId, index) {
  const ids = seriesMembers(all)[seriesId] || [];
  if (ids.length < 2) return [];
  const labels = shortLabels(all);
  const name = (id) => (all.find((t) => t.id === id) || getTargetInfo(id)).name;
  return ids.map((id) => `    ${labels[id] || name(id)}: ${fmtMs(data.byTarget[id]?.[index] ?? null)}`);
}

// The probes the outage rules judge each target by (Settings, Outage rules), which the live loss lane counts too.
const judged = () => val(settingsValues(), 'classifier.window_probes', 10);

// The loss lane's tooltip: how much was lost and at which target (the worst one at that moment in `data`).
function lossLabel(all, data, live, index, pct) {
  const id = data.lossBy[index];
  const where = id ? ` at ${shortLabels(all)[id] || getTargetInfo(id).name}` : '';
  return ` Lost ${Number(pct.toFixed(1))}% ${live ? `of the last ${judged()} probes` : 'of the probes'}${where}`;
}

// The right axis is worth its labels only when the download moves: a flat line near 0 would get three tick labels
// ("0, 50, 100 Mbps") that say nothing. Without them the faint line is still drawn (the legend names it).
const WAN_AXIS_MIN_PEAK_MBPS = 5;
const wanAxisShown = (data) => Math.max(0, ...data.wan.filter(has)) >= WAN_AXIS_MIN_PEAK_MBPS;

// A line's newest reading as the legend says it ("12.3 ms"), '' before its first reply.
function newest(values) {
  const v = [...(values || [])].reverse().find(has);
  return v === undefined ? '' : `${(+v).toFixed(1)} ms`;
}

// What each line is, in words: the series' short name (the path health chain's), then the configured names of the
// targets it is made of (muted, whole text in the tooltip). The note under it says what the chart leaves out.
// Items: { key, kind: line | band | wan | lost, color (CSS), label, sub, title }.
function legend(data, mode, all, quiet) {
  const labels = shortLabels(all);
  const items = [];
  let note = '';
  if (mode === 'targets') {
    pingTargets(all).filter((t) => data.byTarget[t.id]).forEach((t) => {
      items.push({ key: `t:${t.id}`, kind: 'line', color: t.color, label: labels[t.id] || t.name, title: t.name, now: newest(data.byTarget[t.id]) });
    });
    note = 'One line per probed target, in path order. DNS lookups time a name lookup, not a round trip: see Hop details.';
  } else {
    const members = seriesMembers(all);
    const byId = (id) => all.find((t) => t.id === id);
    SERIES.filter((s) => data.series[s.id]).forEach((s) => {
      const ids = members[s.id] || [];
      const names = ids.map((id) => (byId(id) || getTargetInfo(id)).name);
      let sub;
      // the members by address when each has its own (short), else by configured name
      const memberTargets = ids.map(byId).filter(Boolean);
      const shown = memberTargets.length === ids.length ? memberNames(memberTargets, all) : names;
      if (s.combine === 'median' && ids.length > 1) sub = `median of ${shown.join(', ')}`;
      else if (ids.length > 1) sub = `worst of ${ids.length}`;
      else sub = names[0] || '';
      const title = ids.length > 1 ? `${s.name}: ${s.combine === 'median' ? 'median' : 'worst'} of ${names.join(', ')}` : `${s.name}: ${sub}`;
      items.push({ key: s.id, kind: 'line', color: `var(${s.color})`, label: s.name, dash: !!s.dash, sub, title, now: newest(data.series[s.id]) });
    });
    if (data.lo.some(has)) items.push({ key: 'band', kind: 'band', color: 'var(--s2)', label: 'p10–p90 band' });
    const isp = all.filter((t) => t.enabled !== false && t.category === 'isp').length;
    const usesDns = (members.path_internet || []).some((id) => (byId(id) || getTargetInfo(id)).category === 'dns');
    const other = all.filter((t) => t.enabled !== false && t.category === 'custom').length;
    const under = [isp ? `${isp} ISP hop${isp > 1 ? 's' : ''}` : '', other ? `${other} other target${other > 1 ? 's' : ''}` : ''].filter(Boolean);
    note = `Ping round trip to each stage of the path${usesDns ? ' (Internet: DNS lookups, as no public server is pinged)' : ''}.`
      + `${under.length ? ` The ${under.join(' and the ')} ${isp + other > 1 ? 'are' : 'is'} under Each target.` : ''}`;
  }
  // points but not one reply: the lines are empty because every probe was lost, which the axes alone do not say
  const replies = [...Object.values(data.series), ...Object.values(data.byTarget)].some((line) => line.some(has));
  // (while this host cannot send pings nothing is probed: that is not an outage, and the note must not suggest one)
  if (data.times.length && !replies) note = quiet ? 'Not probing: pings are not allowed on this computer, so there is no line to draw.'
    : 'No replies in this time: every probe was lost or timed out, so there is no line to draw. The lane below marks the lost probes.';
  if (data.wan.some(has)) items.push({ key: 'wan', kind: 'wan', color: 'var(--ink-4)', label: `WAN download${wanAxisShown(data) ? ' (right axis)' : ''}` });
  items.push({ key: 'lost', kind: 'lost', color: 'var(--crit)', label: 'Lost probes (lane below)' });
  return { items, note };
}

// The view model of the card: { labels, lines, band, wan, loss, incidents, legend, note, modes, memberLines(id, i),
// lossLabel(i, pct) }, from the data, the line mode and the targets (all of them, in path order, with their colors).
// `live` given (a fixture) is fixed; otherwise the tooltip reads the range when it shows, as the card does not redraw
// between a click on Live and the first live message.
export function present({ data = view, mode = lineMode, all = targetList(), live, quiet = noPings, windows = incidents } = {}) {
  const fixed = live;
  const liveNow = () => (fixed === undefined ? state.currentMode === 'live' : fixed);
  live = liveNow();
  const span = data.times.length > 1 ? data.times[data.times.length - 1] - data.times[0] : 0;
  const { lines: drawn, band } = lines(data, mode, all);
  const words = legend(data, mode, all, quiet);
  return {
    labels: data.times.map((t) => clock(t, live || span < 7200)),
    times: data.times,
    lines: drawn,
    band,
    wan: data.wan.some(has) ? { data: data.wan, axis: wanAxisShown(data) } : null,
    loss: data.loss,
    incidents: windows,
    legend: words.items,
    note: words.note,
    modes: [
      { action: 'setLatencyView', arg: 'path', label: 'Path', title: 'Three lines: router, line and internet', pressed: mode === 'path' },
      { action: 'setLatencyView', arg: 'targets', label: 'Each target', title: 'One line per probed target, ISP hops included', pressed: mode === 'targets' }
    ],
    memberLines: (seriesId, index) => memberLines(all, data, seriesId, index),
    lossLabel: (index, pct) => lossLabel(all, data, liveNow(), index, pct)
  };
}

// Made-up data for the parity test of the card's views (tests/ui/steps/core.mjs): a minute of the three path series.
export function fixture() {
  const all = [
    { id: 'fx_router', name: 'Example router', category: 'local', address: '192.168.88.1', enabled: true, color: '#3987e5' },
    { id: 'fx_modem', name: 'Example modem', category: 'gateway', address: '192.168.88.254', enabled: true, color: '#e5a03a' },
    { id: 'fx_public', name: 'Example resolver', category: 'public', address: '192.0.2.53', enabled: true, color: '#3ac0a0' },
    { id: 'fx_isp', name: 'Example provider router', category: 'isp', address: '198.51.100.1', enabled: true, color: '#c05ac0' }
  ];
  const times = Array.from({ length: 60 }, (_, i) => 1767225600 + i);
  const wave = (base) => times.map((_, i) => (i === 40 ? null : base + (i % 7) / 10));
  const data = {
    times, series: { path_router: wave(1.2), path_line: wave(4.5), path_internet: wave(14) },
    byTarget: { fx_router: wave(1.2), fx_modem: wave(4.5), fx_public: wave(14), fx_isp: wave(8) },
    lo: times.map(() => null), hi: times.map(() => null), loss: times.map((_, i) => (i === 40 ? 10 : 0)), lossBy: times.map((_, i) => (i === 40 ? 'fx_modem' : null)), wan: times.map(() => null)
  };
  return present({ data, mode: 'path', all, live: true, quiet: false, windows: [] });
}

// Hands the card the data now.
export function renderCharts() {
  if (card) card.update(() => present()); // (made only when the card is drawn: not while it is off screen or on a hidden tab)
}

// Switches between the three path series and one line per target.
export function setLineMode(mode) {
  lineMode = mode === 'targets' ? 'targets' : 'path';
  try { localStorage.setItem(VIEW_KEY, lineMode); } catch { /* storage blocked */ }
  renderCharts();
}

// Live: one point per message from the targets and the WAN rate that ride on it. A target's value is its newest probe
// (null when that probe got no answer; last_ms keeps the last answer, which would hide a loss).
function updateLiveCharts(message) {
  noPings = (message.targets || []).some((t) => t.enabled !== false && t.probing_unavailable === true);
  if (state.currentMode !== 'live') return;
  const { timestamp, targets } = message;
  const wanDevice = primaryDevice('wan_counters');
  const router = wanDevice && message.devices ? message.devices[wanDevice.id] : null;
  const byId = new Map(targets.map((t) => [t.target_id, t]));
  const newest = (t) => { const r = t.recent_latencies; return r && r.length ? r[r.length - 1] : (t.last_ms ?? null); };
  const known = targetList().length ? targetList() : targets.map((t) => ({ id: t.target_id, category: t.category }));
  const members = seriesMembers(known);
  const pad = () => new Array(view.times.length).fill(null);
  known.filter((t) => t.category !== 'dns' && byId.has(t.id)).forEach((t) => {
    if (!view.byTarget[t.id]) view.byTarget[t.id] = pad();
    view.byTarget[t.id].push(newest(byId.get(t.id)));
  });
  SERIES.forEach((s) => {
    const ids = members[s.id];
    if (!ids) return;
    if (!view.series[s.id]) view.series[s.id] = pad();
    view.series[s.id].push(combine(s, ids.filter((id) => byId.has(id)).map((id) => newest(byId.get(id)))));
  });
  // loss: the share of the judged probes (10 by default) lost, worst path target (clears by itself after a blip)
  let loss = 0;
  let lossBy = null;
  Object.values(members).flat().filter((id) => byId.has(id)).forEach((id) => {
    const recent = (byId.get(id).recent_latencies || []).slice(-judged());
    const pct = recent.length ? Math.round((recent.filter((x) => x === null).length / recent.length) * 100) : 0;
    if (pct > loss) { loss = pct; lossBy = id; }
  });
  view.times.push(timestamp);
  view.loss.push(loss);
  view.lossBy.push(lossBy);
  view.lo.push(null);
  view.hi.push(null);
  const rate = router ? router.wan_rx_rate_mbps : null;
  const rx = has(rate) ? Math.round(rate * 10) / 10 : null;
  view.wan.push(rx);
  while (view.times.length > maxLivePoints) {
    view.times.shift();
    view.loss.shift();
    view.lossBy.shift();
    view.lo.shift();
    view.hi.shift();
    view.wan.shift();
    Object.values(view.series).forEach((a) => a.shift());
    Object.values(view.byTarget).forEach((a) => a.shift());
  }
  renderCharts();
}

// The card, the 1 s feed, and the incident windows the incident log publishes.
export function mountCharts(bus, section) {
  card = createCard({ panel: 'latency', root: section.querySelector('.panel-b'), standard, fixture, offscreen: 'defer' });
  renderCharts();
  document.addEventListener('zonechange', renderCharts); // the time axis is on the page's clock (a history view gets no messages)
  let listed = [];
  const shade = () => {
    incidents = listed.filter((i) => i.kind !== 'service_down').map((i) => ({
      start: i.start_time, end: i.end_time ?? Date.now() / 1000,
      level: i.expected ? 'busy' : i.severity === 'critical' ? 'crit' : 'warn'
    }));
    renderCharts();
  };
  bus.subscribe('incidents:list', (list) => { listed = list || []; shade(); });
  everyWhileVisible(shade, 60000); // (an ongoing incident's shading reaches "now")
  return bus.subscribe('message', updateLiveCharts);
}
