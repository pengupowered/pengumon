// The latency panel's charts: one latency chart and a thin loss lane under it. Two ways to draw the latency:
//   path (default): three series in path order (router, line, internet; path.js), the internet's p10-p90 band when the
//     server sends it, each series named at its line end. The legend says which targets each series is made of, and
//     the tooltip gives every member's own value.
//   each target: one thin line per pinged target (ISP hops included, DNS lookups left out: they time a lookup, not a
//     round trip), in the targets' own colours (the ones Hop details uses).
// Both get the WAN download as a faint background on the right axis and the incident windows shaded. Chart.js, with
// text and grid colours from the theme (chart-theme.js) and series colours from the tokens.
//
// One view model feeds both charts: { times, series: {id: [ms]}, byTarget: {targetId: [ms]}, lo, hi, loss, lossBy, wan }.
// The live feed appends a point per message (charts.js); the history views replace it (history.js).

import { maxLivePoints, state } from '../../js/state.js';
import { escapeHtml, getTargetInfo, targetList } from '../../js/util.js';
import { SERIES, combine, memberNames, seriesMembers, shortLabels } from '../../js/path.js';
import { token } from '../../js/chart-theme.js';
import { primaryDevice } from '../../js/devices.js';

export const view = { times: [], series: {}, byTarget: {}, lo: [], hi: [], loss: [], lossBy: [], wan: [] };

// Which lines the chart draws: 'path' or 'targets'; kept per browser (a convenience, so failures are ignored).
const VIEW_KEY = 'pengumon.latency.view';
let lineMode = 'path';
try { if (localStorage.getItem(VIEW_KEY) === 'targets') lineMode = 'targets'; } catch { /* storage blocked */ }

// The pinged targets in path order (DNS lookups are not round trips and stay out of the chart).
export const pingTargets = () => targetList().filter((t) => t.enabled !== false && t.category !== 'dns');
let noPings = false; // the live targets say this host cannot send pings (probing_unavailable)
let incidents = []; // [{start, end, level, label}] for the shaded windows

const has = (v) => v !== null && v !== undefined;
// One formatter per format, made on first use: with options, every toLocaleTimeString call built a new Intl.DateTimeFormat,
// and the chart relabels up to 240 points on every message.
const CLOCK = { hm: null, hms: null };
const clock = (ts, seconds) => {
  const k = seconds ? 'hms' : 'hm';
  CLOCK[k] ||= new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) });
  return CLOCK[k].format(ts * 1000);
};

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

// Index of the point nearest to `ts` (times are sorted).
function nearest(ts) {
  const t = view.times;
  let lo = 0;
  let hi = t.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (t[mid] < ts) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// Shaded incident windows behind the lines, and each series' name at its line end.
const pathPlugin = {
  id: 'pathDecor',
  beforeDatasetsDraw(chart) {
    if (!view.times.length || !incidents.length) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    const t0 = view.times[0];
    const t1 = view.times[view.times.length - 1];
    incidents.forEach((inc) => {
      if (inc.end < t0 || inc.start > t1) return;
      const x0 = x.getPixelForValue(nearest(Math.max(inc.start, t0)));
      const x1 = x.getPixelForValue(nearest(Math.min(inc.end, t1)));
      const color = token(inc.level === 'crit' ? '--crit' : inc.level === 'busy' ? '--busy' : '--warn');
      ctx.save();
      ctx.globalAlpha = 0.12;
      ctx.fillStyle = color;
      ctx.fillRect(x0, a.top, Math.max(2, x1 - x0), a.bottom - a.top);
      ctx.globalAlpha = 0.8;
      ctx.strokeStyle = color;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x0 + 0.5, a.top);
      ctx.lineTo(x0 + 0.5, a.bottom);
      ctx.stroke();
      ctx.restore();
    });
  },
  afterDatasetsDraw(chart) {
    const { ctx, chartArea: a } = chart;
    if (chart.width < 480) return; // a phone: the legend names the lines, the right margin goes to the chart
    const ends = [];
    chart.data.datasets.forEach((d, i) => {
      if (!d.pathLabel || !chart.isDatasetVisible(i)) return;
      const meta = chart.getDatasetMeta(i);
      let k = d.data.length - 1;
      while (k >= 0 && !has(d.data[k])) k--;
      if (k < 0 || !meta.data[k]) return;
      ends.push({ label: d.pathLabel, color: d.borderColor, x: meta.data[k].x, y: meta.data[k].y, y0: meta.data[k].y });
    });
    ends.sort((p, q) => p.y - q.y);
    for (let k = 1; k < ends.length; k++) if (ends[k].y - ends[k - 1].y < 14) ends[k].y = ends[k - 1].y + 14;
    const load = chart.scales.yLoad;
    const labelX = (load && load.options.display ? load.right : a.right) + 8;
    ctx.save();
    ctx.font = `600 11px ${token('--font')}`;
    ctx.textBaseline = 'middle';
    ends.forEach((e) => {
      ctx.fillStyle = e.color;
      ctx.strokeStyle = token('--surface-1');
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(e.x, e.y0, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = token('--ink-2');
      ctx.fillText(e.label, labelX, e.y);
    });
    ctx.restore();
  }
};

const fmtMs = (v) => (has(v) ? `${v.toFixed(1)} ms` : 'no reply');

function datasets() {
  const sets = [];
  if (lineMode === 'targets') {
    const labels = shortLabels(targetList());
    pingTargets().forEach((t) => {
      if (!view.byTarget[t.id]) return;
      const color = getTargetInfo(t.id).color;
      sets.push({ id: `t:${t.id}`, target: t.id, label: labels[t.id] || t.name, data: view.byTarget[t.id], borderColor: color, backgroundColor: color,
        borderWidth: 1.5, pointRadius: 0, tension: 0.25, spanGaps: false, order: 1 });
    });
  } else {
    SERIES.forEach((s) => {
      if (!view.series[s.id]) return;
      const color = token(s.color);
      if (s.id === 'path_internet' && view.lo.some(has)) {
        sets.push({ id: 'band_lo', label: 'p10', data: view.lo, borderWidth: 0, pointRadius: 0, fill: false, spanGaps: false, order: 5 });
        sets.push({ id: 'band_hi', label: 'p10–p90', data: view.hi, borderWidth: 0, pointRadius: 0, backgroundColor: `${color}26`, fill: '-1', spanGaps: false, order: 5 });
      }
      sets.push({ id: s.id, label: s.name, pathLabel: s.name, data: view.series[s.id], borderColor: color, backgroundColor: color,
        borderWidth: 2, pointRadius: 0, tension: 0.25, spanGaps: false, order: 1 });
    });
  }
  if (view.wan.some(has)) {
    sets.push({ id: 'wan_load', label: 'WAN download', data: view.wan, yAxisID: 'yLoad', borderColor: token('--ink-4'),
      backgroundColor: `${token('--ink-4')}1f`, fill: 'origin', borderWidth: 1, borderDash: [4, 3], pointRadius: 0, spanGaps: true, tension: 0.2, order: 10 });
  }
  return sets;
}

// The tooltip of a path series: its value, then each member's own value at that moment (a median of three says
// little until you see the three).
function memberLines(seriesId, index) {
  const ids = seriesMembers(targetList())[seriesId] || [];
  if (ids.length < 2) return [];
  const labels = shortLabels(targetList());
  return ids.map((id) => `    ${labels[id] || getTargetInfo(id).name}: ${fmtMs(view.byTarget[id]?.[index] ?? null)}`);
}

// What each line is, in words: the series' short name (the path health chain's), then the configured names of the
// targets it is made of (muted, whole text in the tooltip). The note under it says what the chart leaves out.
function legend() {
  const box = document.getElementById('latencyLegend');
  const note = document.getElementById('latencyNote');
  if (!box) return;
  const all = targetList();
  const labels = shortLabels(all);
  const name = (id) => getTargetInfo(id).name;
  const parts = [];
  let noteText = '';
  if (lineMode === 'targets') {
    pingTargets().filter((t) => view.byTarget[t.id]).forEach((t) => {
      parts.push(`<span class="lg" title="${escapeHtml(t.name)}"><i class="key line" style="--c:${escapeHtml(getTargetInfo(t.id).color)}"></i><b>${escapeHtml(labels[t.id] || t.name)}</b></span>`);
    });
    noteText = 'One line per pinged target, in path order. DNS lookups time a name lookup, not a round trip: see Hop details.';
  } else {
    const members = seriesMembers(all);
    SERIES.filter((s) => view.series[s.id]).forEach((s) => {
      const ids = members[s.id] || [];
      const names = ids.map(name);
      let sub;
      // the members by address when each has its own (short), else by configured name
      const memberTargets = ids.map((id) => all.find((t) => t.id === id)).filter(Boolean);
      const shown = memberTargets.length === ids.length ? memberNames(memberTargets, all) : names;
      if (s.combine === 'median' && ids.length > 1) sub = `median of ${shown.join(', ')}`;
      else if (ids.length > 1) sub = `worst of ${ids.length}`;
      else sub = names[0] || '';
      const title = ids.length > 1 ? `${s.name}: ${s.combine === 'median' ? 'median' : 'worst'} of ${names.join(', ')}` : `${s.name}: ${sub}`;
      parts.push(`<span class="lg" title="${escapeHtml(title)}"><i class="key line" style="--c:var(${s.color})"></i><b>${s.name}</b><span class="muted">${escapeHtml(sub)}</span></span>`);
    });
    if (view.lo.some(has)) parts.push('<span class="lg"><i class="key" style="--c:var(--s2);opacity:.3"></i>p10–p90 band</span>');
    const isp = all.filter((t) => t.enabled !== false && t.category === 'isp').length;
    const usesDns = (members.path_internet || []).some((id) => getTargetInfo(id).category === 'dns');
    const other = all.filter((t) => t.enabled !== false && t.category === 'custom').length;
    const under = [isp ? `${isp} ISP hop${isp > 1 ? 's' : ''}` : '', other ? `${other} other target${other > 1 ? 's' : ''}` : ''].filter(Boolean);
    noteText = `Ping round trip to each stage of the path${usesDns ? ' (Internet: DNS lookups, as no public server is pinged)' : ''}.`
      + `${under.length ? ` The ${under.join(' and the ')} ${isp + other > 1 ? 'are' : 'is'} under Each target.` : ''}`;
  }
  // points but not one reply: the lines are empty because every probe was lost, which the axes alone do not say
  const replies = [...Object.values(view.series), ...Object.values(view.byTarget)].some((line) => line.some(has));
  // (while this host cannot send pings nothing is probed: that is not an outage, and the note must not suggest one)
  if (view.times.length && !replies) noteText = noPings ? 'Not probing: pings are not allowed on this computer, so there is no line to draw.'
    : 'No replies in this time: every probe was lost or timed out, so there is no line to draw. The lane below marks the lost probes.';
  if (view.wan.some(has)) parts.push(`<span class="lg"><i class="key" style="--c:var(--ink-4);opacity:.5"></i>WAN download${wanAxisShown() ? ' (right axis)' : ''}</span>`);
  parts.push('<span class="lg"><i class="key" style="--c:var(--crit);width:3px;height:11px"></i>Lost probes (lane below)</span>');
  const html = parts.join('');
  if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
  if (note && note.textContent !== noteText) note.textContent = noteText;
}

// Switches between the three path series and one line per target.
export function setLineMode(mode) {
  lineMode = mode === 'targets' ? 'targets' : 'path';
  try { localStorage.setItem(VIEW_KEY, lineMode); } catch { /* storage blocked */ }
  markLineMode();
  renderCharts();
}

function markLineMode() {
  document.querySelectorAll('#latencyViewButtons button').forEach((b) => {
    const on = b.dataset.arg === lineMode;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

// The right axis is worth its labels only when the download moves: a flat line near 0 would get three tick labels
// ("0, 50, 100 Mbps") that say nothing. Without them the faint line is still drawn (the legend names it).
const WAN_AXIS_MIN_PEAK_MBPS = 5;
const wanAxisShown = () => Math.max(0, ...view.wan.filter(has)) >= WAN_AXIS_MIN_PEAK_MBPS;

// Pushes the view model into both charts.
export function renderCharts() {
  if (!state.latencyChart) return;
  const live = state.currentMode === 'live';
  const span = view.times.length > 1 ? view.times[view.times.length - 1] - view.times[0] : 0;
  const labels = view.times.map((t) => clock(t, live || span < 7200));
  state.latencyChart.data.labels = labels;
  state.latencyChart.data.datasets = datasets();
  state.latencyChart.options.scales.yLoad.display = wanAxisShown();
  state.lossChart.data.labels = labels;
  state.lossChart.data.datasets = [{ id: 'loss', label: 'Lost', data: view.loss.map((v) => (v > 0 ? v : null)), backgroundColor: token('--crit'),
    borderWidth: 0, barPercentage: 0.9, categoryPercentage: 1, minBarLength: 3 }];
  state.latencyChart.update('none');
  alignLossLane();
  state.lossChart.update('none');
  legend();
}

// The loss lane's plot area starts and ends where the latency chart's does (its right axis and line-end names included),
// so a lost probe sits under the moment it belongs to.
function alignLossLane() {
  const lat = state.latencyChart;
  if (!lat.chartArea) return;
  state.lossChart.options.layout.padding.right = Math.max(0, lat.width - lat.chartArea.right);
}

// The loss lane's tooltip: how much was lost and at which target (the worst one at that moment).
function lossLabel(c) {
  const id = view.lossBy[c.dataIndex];
  const where = id ? ` at ${shortLabels(targetList())[id] || getTargetInfo(id).name}` : '';
  const live = state.currentMode === 'live';
  return ` Lost ${Number(c.parsed.y.toFixed(1))}% ${live ? 'of the last 10 probes' : 'of the probes'}${where}`;
}

// An empty loss lane says so, instead of looking like a chart that failed to load.
const quietLane = {
  id: 'quietLane',
  afterDraw(chart) {
    if (!view.times.length || view.loss.some((v) => v > 0)) return;
    const { ctx, chartArea: a } = chart;
    ctx.save();
    ctx.font = `11px ${token('--font')}`;
    ctx.fillStyle = token('--ink-3');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('no lost probes in this window', (a.left + a.right) / 2, (a.top + a.bottom) / 2);
    ctx.restore();
  }
};

// How many time labels fit across a chart this wide.
const timeTicks = (width) => (width < 480 ? 3 : width < 800 ? 4 : 6);

export function initCharts() {
  state.latencyChart = new Chart(document.getElementById('latencyChart'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    plugins: [pathPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { right: window.innerWidth < 520 ? 0 : 70, top: 6 } },
      interaction: { mode: 'index', intersect: false },
      // fewer time labels on a narrow chart (a phone): six "HH:MM:SS" labels do not fit in ~200 px; no line-end names
      onResize: (chart, size) => {
        chart.options.scales.x.ticks.maxTicksLimit = timeTicks(size.width);
        chart.options.layout.padding.right = size.width < 480 ? 0 : 70;
        if (state.lossChart) requestAnimationFrame(() => { alignLossLane(); state.lossChart.update('none'); });
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: timeTicks(window.innerWidth), maxRotation: 0, autoSkipPadding: 16 } },
        // a hard floor: one bad reading must not move 0 ms to mid-chart
        y: { min: 0, suggestedMax: 30, ticks: { callback: (v) => `${v} ms`, maxTicksLimit: 5 }, border: { display: false } },
        yLoad: { position: 'right', min: 0, suggestedMax: 100, grid: { drawOnChartArea: false }, display: false,
          ticks: { callback: (v) => `${v} Mbps`, maxTicksLimit: 4 }, border: { display: false } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          // the p10-p90 band has no reading of its own; a WAN gap (no router reading near that moment) is left out too
          filter: (item) => !String(item.dataset.id).startsWith('band_') && !(item.dataset.id === 'wan_load' && !has(item.parsed.y)),
          callbacks: {
            label: (c) => (c.dataset.id === 'wan_load' ? ` WAN download: ${c.parsed.y} Mbps`
              : ` ${c.dataset.label}: ${fmtMs(c.parsed.y)}`),
            afterLabel: (c) => (String(c.dataset.id).startsWith('path_') ? memberLines(c.dataset.id, c.dataIndex) : [])
          }
        }
      }
    }
  });
  state.lossChart = new Chart(document.getElementById('lossChart'), {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { right: 70 } },
      scales: {
        x: { display: false },
        y: { min: 0, max: 100, afterFit: (axis) => { axis.width = Math.max(axis.width, state.latencyChart?.scales.y?.width ?? 0); },
          ticks: { stepSize: 100, callback: (v) => (v === 100 ? 'lost' : '') }, grid: { drawOnChartArea: true }, border: { display: false } }
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: lossLabel } } }
    },
    plugins: [quietLane]
  });
}

// Live: one point per message from the targets and the WAN rate that ride on it. A target's value is its newest probe
// (null when that probe got no answer; last_ms keeps the last answer, which would hide a loss).
export function updateLiveCharts(message) {
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
  // loss: the share of the last 10 probes lost, worst path target (clears by itself after a blip)
  let loss = 0;
  let lossBy = null;
  Object.values(members).flat().filter((id) => byId.has(id)).forEach((id) => {
    const recent = (byId.get(id).recent_latencies || []).slice(-10);
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

// The 1 s feed, and the incident windows the incident log publishes.
export function mountCharts(bus) {
  markLineMode();
  bus.subscribe('incidents:list', (list) => {
    incidents = (list || []).filter((i) => i.kind !== 'service_down').map((i) => ({
      start: i.start_time, end: i.end_time ?? Date.now() / 1000,
      level: i.expected ? 'busy' : i.severity === 'critical' ? 'crit' : 'warn'
    }));
    if (state.latencyChart) state.latencyChart.update('none');
  });
  return bus.subscribe('message', updateLiveCharts);
}
