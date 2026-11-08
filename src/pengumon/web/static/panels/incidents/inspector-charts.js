// The inspector's synced tracks: latency and loss per hop, the line's signal, the router. All four share one category
// axis (one label per second of the window), the same left and right gutters, the shaded band of the incident and a
// crosshair that follows the pointer on every chart. Colors come from the design tokens (chart-theme.js).

import { escapeHtml, localFormat, getTargetInfo, sortTargetsByCanonical, targetList } from '../../js/util.js';
import { shortLabels } from '../../js/path.js';
import { series, token } from '../../js/chart-theme.js';

const AXIS = 56; // the gutter, left and right, so the tracks line up whatever they plot
const charts = { lat: null, loss: null, line: null, router: null };
let band = null; // { i0, i1, color } of the incident, as label indexes
let hover = null; // label index under the pointer, on any chart

const nearest = (list, ts, within) => {
  let best = null;
  let diff = Infinity;
  for (const item of list) {
    const d = Math.abs(item.timestamp - ts);
    if (d < diff) { diff = d; best = item; }
  }
  return best && diff <= within ? best : null;
};
const num = (v) => (v === null || v === undefined ? null : v);

// Probe rounds. Every target is probed once per round, 25 ms apart, so one round's samples lie within a fraction of a
// second; but a timeout delays the next round, and rounding timestamps to whole seconds then splits a round across two
// labels, leaving holes that read as lost (or as missing) probes. A round starts at its first sample and ends when a
// target repeats or 0.6 s have passed. Bucketed history (long incidents) has one timestamp per bucket: one round each.
function probeRounds(samples) {
  const out = [];
  let round = null;
  [...samples].sort((a, b) => a.timestamp - b.timestamp).forEach((s) => {
    if (!round || s.timestamp - round.t > 0.6 || round.by[s.target_id]) {
      round = { t: s.timestamp, by: {} };
      out.push(round);
    }
    round.by[s.target_id] = s;
  });
  return out;
}

// One target in one round: no sample (null lat, no loss), lost (null lat, loss %), or answered (lat, loss % of a
// bucket that lost some of its probes).
function probeReading(s) {
  if (!s) return { state: 'none', lat: null, loss: 0 };
  const loss = s.packet_loss_pct || 0;
  if (!s.success || s.latency_ms === null || s.latency_ms === undefined) return { state: 'lost', lat: null, loss: loss || 100 };
  return { state: 'ok', lat: s.latency_ms, loss };
}

const pctText = (v) => `${Number(v.toFixed(1))}%`;
const msText = (v) => `${v < 10 ? v.toFixed(2) : v.toFixed(1)} ms`;
function readingText(r) {
  if (r.state === 'none') return 'no probe';
  if (r.state === 'lost') return 'lost';
  return r.loss > 0 ? `${msText(r.lat)} (lost ${pctText(r.loss)})` : msText(r.lat);
}

// A point with gaps on both sides draws no line, so it gets a dot of its own.
const lonePoint = (c) => {
  const d = c.dataset.data;
  const i = c.dataIndex;
  return d[i] !== null && d[i - 1] == null && d[i + 1] == null ? 2.5 : 0;
};

// Draws the incident band behind the data and the shared crosshair over it; reports the pointer to the other charts.
const syncPlugin = {
  id: 'incidentSync',
  beforeDatasetsDraw(chart) {
    if (!band) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    const step = x.getPixelForValue(1) - x.getPixelForValue(0) || 0;
    const left = Math.max(a.left, x.getPixelForValue(band.i0) - step / 2);
    const right = Math.min(a.right, x.getPixelForValue(band.i1) + step / 2);
    ctx.save();
    ctx.fillStyle = token(band.color);
    ctx.fillRect(left, a.top, Math.max(2, right - left), a.bottom - a.top);
    ctx.strokeStyle = token(band.edge);
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(left, a.top);
    ctx.lineTo(left, a.bottom);
    ctx.stroke();
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    if (hover === null) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    const px = x.getPixelForValue(hover);
    if (!(px >= a.left && px <= a.right)) return;
    ctx.save();
    ctx.strokeStyle = token('--ink-3');
    ctx.beginPath();
    ctx.moveTo(px, a.top);
    ctx.lineTo(px, a.bottom);
    ctx.stroke();
    ctx.restore();
  },
  afterEvent(chart, args) {
    const e = args.event;
    if (e.type !== 'mousemove' && e.type !== 'mouseout') return;
    const next = e.type === 'mouseout' || !args.inChartArea ? null : Math.round(chart.scales.x.getValueForPixel(e.x));
    if (next === hover) return;
    hover = next;
    Object.values(charts).forEach((c) => { if (c && c !== chart) c.draw(); });
  }
};

function baseOptions({ right = false } = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'index', intersect: false },
    layout: { padding: { right: right ? 0 : AXIS } },
    scales: {
      x: { grid: {}, ticks: { maxTicksLimit: 8, font: { size: 10 }, maxRotation: 0 } }
    },
    plugins: { legend: { display: false } }
  };
}

const yAxis = (extra = {}) => ({
  grid: {}, ticks: { font: { size: 10 } }, afterFit: (s) => { s.width = AXIS; }, ...extra
});

function kill() {
  Object.keys(charts).forEach((k) => {
    if (charts[k]) { try { charts[k].destroy(); } catch (e) { /* already gone */ } charts[k] = null; }
  });
}

const key = (color, text, dash, title) => `<span${title ? ` title="${escapeHtml(title)}"` : ''}><i class="key line" style="--c:${color}${dash ? ';background:none;border-top:2px dashed var(--c);height:0' : ''}"></i>${escapeHtml(text)}</span>`;

// Draws the tracks for one inspection; returns nothing. data is the /inspect response.
export function renderTracks(data) {
  kill();
  hover = null;
  const canvasLat = document.getElementById('incidentZoomChart');
  const canvasLoss = document.getElementById('incidentZoomLossChart');
  if (!data || !data.samples || !data.samples.length || !canvasLat || !canvasLoss) return;

  // one entry per probe round, per target
  const targetSet = new Set(data.samples.map((s) => s.target_id));
  const groups = probeRounds(data.samples);
  const times = groups.map((g) => g.t);
  const labels = times.map((t) => localFormat(t * 1000, { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  const inside = times.map((t, i) => [t, i]).filter(([t]) => t >= data.event_start && t <= data.event_end).map(([, i]) => i);
  const expected = data.incident && data.incident.expected;
  band = inside.length
    ? { i0: inside[0], i1: inside[inside.length - 1], color: expected ? '--busy-bg' : data.incident.severity === 'critical' ? '--crit-bg' : '--warn-bg', edge: expected ? '--busy' : data.incident.severity === 'critical' ? '--crit' : '--warn' }
    : null;

  const ids = sortTargetsByCanonical(targetSet);
  const labelOf = shortLabels(targetList()); // the page's short names (Router, Gateway, ...); the configured one on hover
  const latSets = [];
  const lossSets = [];
  ids.forEach((tid) => {
    const info = getTargetInfo(tid);
    const short = labelOf[tid] || info.name;
    const reading = groups.map((g) => probeReading(g.by[tid]));
    // a lost probe breaks the line rather than being bridged by one drawn from its neighbors
    latSets.push({ label: short, data: reading.map((r) => r.lat), readings: reading, borderColor: info.color, backgroundColor: info.color, borderWidth: 2, pointRadius: lonePoint, pointHoverRadius: 4, spanGaps: false, tension: 0.2 });
    // bars of one round overlap; the nearest hop is drawn on top (Chart.js draws a lower `order` last), so the color
    // shows where the loss starts and the tooltip lists the rest
    lossSets.push({ label: short, data: reading.map((r) => (r.loss > 0 ? r.loss : null)), order: lossSets.length, backgroundColor: info.color, borderColor: info.color, borderWidth: 1, barPercentage: 1, categoryPercentage: 1, minBarLength: 4 });
  });
  document.getElementById('inspLatLegend').innerHTML = ids.map((tid) => { const i = getTargetInfo(tid); return key(i.color, labelOf[tid] || i.name, false, i.name); }).join('');

  const plugins = [syncPlugin];
  const lat = baseOptions();
  lat.scales.y = yAxis({ beginAtZero: true, title: { display: true, text: 'RTT (ms)', font: { size: 10 } }, ticks: { font: { size: 10 }, callback: (v) => `${v} ms` } });
  lat.plugins.tooltip = { callbacks: { label: (c) => `${c.dataset.label}: ${readingText(c.dataset.readings[c.dataIndex])}` } };
  charts.lat = new Chart(canvasLat.getContext('2d'), { type: 'line', data: { labels, datasets: latSets }, options: lat, plugins });

  const loss = baseOptions();
  loss.scales.x.stacked = true;
  loss.scales.y = yAxis({ min: 0, max: 100, ticks: { stepSize: 25, callback: (v) => `${v}%`, font: { size: 10 } }, title: { display: true, text: 'Loss', font: { size: 10 } } });
  // only the targets that lost probes in that round: the others have no bar to describe
  loss.plugins.tooltip = { filter: (item) => item.raw !== null, callbacks: { label: (c) => `${c.dataset.label} lost ${pctText(c.parsed.y)}` } };
  charts.loss = new Chart(canvasLoss.getContext('2d'), { type: 'bar', data: { labels, datasets: lossSets }, options: loss, plugins });

  lineTrack(data, times, labels, plugins);
  routerTrack(data, times, labels, plugins);
  // only the bottom track keeps its time labels: the tracks share one axis
  const order = [charts.lat, charts.loss, charts.line, charts.router].filter(Boolean);
  order.slice(0, -1).forEach((c) => { c.options.scales.x.ticks.display = false; c.update('none'); });
}

// Line signal: the SNR of the worst channel (or the average) as a line, the new uncorrectable errors as bars.
function lineTrack(data, times, labels, plugins) {
  const box = document.getElementById('incidentZoomDocsisContainer');
  const canvas = document.getElementById('incidentZoomDocsisChart');
  const snaps = data.docsis_snapshots || [];
  box.hidden = !snaps.length;
  if (!snaps.length) return;
  const worst = data.docsis_correlation && data.docsis_correlation.worst_channel;
  let hasWorst = false;
  const fec = [];
  const snr = [];
  times.forEach((t) => {
    const s = nearest(snaps, t, 45);
    if (!s) { fec.push(null); snr.push(null); return; }
    const ch = worst && s.channels ? s.channels.find((c) => c.channel_id === worst.channel_id && c.direction === worst.direction) : null;
    if (ch && ch.snr_db !== null && ch.snr_db !== undefined) { hasWorst = true; snr.push(ch.snr_db); } else snr.push(num(s.downstream_snr_avg));
    fec.push(s.uncorrectables_recent || 0);
  });
  const name = hasWorst ? `Ch ${worst.channel_id}${worst.frequency_mhz ? ` (${worst.frequency_mhz} MHz)` : ''} SNR` : 'Average SNR';
  document.getElementById('inspDocsisLegend').outerHTML = `<span class="tools legend" id="inspDocsisLegend">${key('var(--s3)', `${name} (line)`)}${key('var(--crit)', 'new uncorrectable errors (bars)')}</span>`;
  const o = baseOptions({ right: true });
  o.scales.y = yAxis({ position: 'left', title: { display: true, text: 'SNR (dB)', font: { size: 10 } }, ticks: { font: { size: 10 }, callback: (v) => `${v} dB` } });
  o.scales.yErrors = yAxis({ position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, title: { display: true, text: 'new errors', font: { size: 10 } } });
  o.plugins.tooltip = { callbacks: { label: (c) => (c.dataset.yAxisID === 'yErrors' ? `New uncorrectable errors: ${c.parsed.y ?? 0}` : `SNR: ${c.parsed.y === null ? '-' : `${c.parsed.y} dB`}`) } };
  charts.line = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: { labels, datasets: [
      { type: 'bar', label: 'New errors', data: fec, backgroundColor: token('--crit'), borderColor: token('--crit'), borderWidth: 1, barPercentage: 1, categoryPercentage: 1, minBarLength: 3, yAxisID: 'yErrors', order: 2 },
      { type: 'line', label: name, data: snr, borderColor: series(2), backgroundColor: series(2), borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, spanGaps: true, tension: 0.1, yAxisID: 'y', order: 1 }
    ] },
    options: o, plugins
  });
}

// Router: WAN throughput (left axis) and CPU (right axis), from the router device's stored polls.
function routerTrack(data, times, labels, plugins) {
  const box = document.getElementById('incidentZoomRouterContainer');
  const canvas = document.getElementById('incidentZoomRouterChart');
  const snaps = data.router_snapshots || [];
  box.hidden = !snaps.length;
  if (!snaps.length) return;
  const rx = [];
  const tx = [];
  const cpu = [];
  times.forEach((t) => {
    const s = nearest(snaps, t, 45);
    rx.push(s ? num(s.wan_rx_rate_mbps) : null);
    tx.push(s ? num(s.wan_tx_rate_mbps) : null);
    cpu.push(s ? num(s.cpu_usage_pct) : null);
  });
  document.getElementById('inspRouterLegend').outerHTML = `<span class="tools legend" id="inspRouterLegend">${key('var(--s1)', 'WAN down (Mbps)')}${key('var(--s2)', 'WAN up (Mbps)')}${key('var(--s4)', 'CPU % (right axis)', true)}</span>`;
  const o = baseOptions({ right: true });
  o.scales.yMbps = yAxis({ position: 'left', beginAtZero: true, title: { display: true, text: 'Mbps', font: { size: 10 } }, ticks: { font: { size: 10 }, callback: (v) => `${v}M` } });
  o.scales.yCpu = yAxis({ position: 'right', beginAtZero: true, max: 100, grid: { drawOnChartArea: false }, title: { display: true, text: 'CPU', font: { size: 10 } }, ticks: { font: { size: 10 }, callback: (v) => `${v}%` } });
  o.plugins.tooltip = { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y === null ? '-' : c.dataset.yAxisID === 'yCpu' ? `${c.parsed.y.toFixed(0)}%` : `${c.parsed.y.toFixed(1)} Mbps`}` } };
  const line = (label, data, color, axis, extra = {}) => ({ label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, spanGaps: true, tension: 0.1, yAxisID: axis, ...extra });
  charts.router = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [line('WAN down', rx, series(0), 'yMbps'), line('WAN up', tx, series(1), 'yMbps'), line('Router CPU', cpu, series(3), 'yCpu', { borderDash: [3, 3], borderWidth: 1.5 })] },
    options: o, plugins
  });
}
