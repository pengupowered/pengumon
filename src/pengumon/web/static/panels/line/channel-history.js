// Per-channel history of a DOCSIS line (the "History" tab of the channels modal): a heatmap of every channel over time
// on one canvas, colored by SNR (downstream) or power, and a line chart of the channel clicked. Data comes from
// GET /api/devices/{id}/channels/history, fetched when the tab opens and when the range changes.

import { registerActions } from '../../js/dispatch.js';
import { formatClock, setText } from '../../js/util.js';
import { state } from '../../js/state.js';
import { gradeByRange } from '../../js/grades.js';
import { series, token } from '../../js/chart-theme.js';

const ROW_H = 14;
const LABEL_W = 56;
const AXIS_H = 18;
const RANGES = { 86400: '24 h', 604800: '7 d' };

let rangeSeconds = 86400;
let deviceId = null; // the line whose channels are shown (setChannelDevice)
let device = null;
let metric = 'snr'; // 'snr' (downstream only) or 'power'
let data = null; // { start, end, channels: [{key, dir, id, rows}], gap }
let selected = null;
let detailChart = null;
let requestSeq = 0;
let layout = []; // [{key, y}] of the drawn heatmap, for hit testing

const none = () => token('--nodata');
const GRADE_TOKEN = { good: '--good', warn: '--warn-fill', critical: '--crit' };

// A cell's color: the reading graded by the device's ranges from the server (SNR for downstream SNR, power by
// direction); gray without a reading or a range.
export function cellColor(dir, metricName, row) {
  const [, , , power, snr] = row;
  const ranges = (device && device.ranges) || {};
  const grade = metricName === 'snr' ? gradeByRange(ranges.ds_snr, snr)
    : gradeByRange(dir === 'd' ? ranges.ds_power : ranges.us_power, power);
  return grade ? token(GRADE_TOKEN[grade]) : none();
}

function groupChannels(rows, start, end) {
  const map = new Map();
  const stamps = new Set();
  for (const r of rows) {
    const key = `${r[1]}${r[2]}`;
    if (!map.has(key)) map.set(key, { key, dir: r[1], id: r[2], rows: [] });
    map.get(key).rows.push(r);
    stamps.add(r[0]);
  }
  const ts = [...stamps].sort((a, b) => a - b);
  const gaps = ts.slice(1).map((t, i) => t - ts[i]).sort((a, b) => a - b);
  const gap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 900;
  const channels = [...map.values()].sort((a, b) => (a.dir === b.dir ? a.id - b.id : (a.dir === 'd' ? -1 : 1)));
  return { start, end, channels, gap, count: ts.length };
}

const label = (c) => `${c.dir === 'd' ? 'DS' : 'US'} ${c.id}`;

export function drawHeatmap() {
  const canvas = document.getElementById('chHistCanvas');
  const empty = document.getElementById('chHistEmpty');
  if (!canvas || !data) return;
  const channels = data.channels.filter((c) => metric === 'power' || c.dir === 'd');
  empty.hidden = channels.length > 0;
  canvas.hidden = !channels.length;
  layout = [];
  if (!channels.length) return;
  const width = Math.max(320, canvas.parentElement.clientWidth);
  const height = AXIS_H + channels.length * ROW_H;
  canvas.width = width;
  canvas.height = height;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, width, height);
  const plotW = width - LABEL_W;
  const span = Math.max(1, data.end - data.start);
  const cellW = Math.max(1, Math.ceil((plotW * data.gap) / span));
  ctx.font = '10px sans-serif';
  ctx.textBaseline = 'middle';
  channels.forEach((c, i) => {
    const y = AXIS_H + i * ROW_H;
    layout.push({ key: c.key, y });
    ctx.fillStyle = c.key === selected ? token('--accent') : token('--ink-3');
    ctx.fillText(label(c), 2, y + ROW_H / 2);
    ctx.fillStyle = none();
    ctx.fillRect(LABEL_W, y, plotW, ROW_H - 1);
    for (const r of c.rows) {
      const x = LABEL_W + Math.floor(((r[0] - data.start) / span) * plotW);
      ctx.fillStyle = cellColor(c.dir, metric, r);
      ctx.fillRect(x, y, cellW, ROW_H - 1);
    }
    if (c.key === selected) {
      ctx.strokeStyle = token('--accent');
      ctx.strokeRect(LABEL_W - 0.5, y - 0.5, plotW + 1, ROW_H);
    }
  });
  ctx.fillStyle = token('--ink-3');
  ctx.textBaseline = 'top';
  for (let i = 0; i <= 4; i++) {
    const t = data.start + (span * i) / 4;
    const text = formatClock(t * 1000, rangeSeconds > 86400);
    ctx.textAlign = i === 0 ? 'left' : (i === 4 ? 'right' : 'center');
    ctx.fillText(text, LABEL_W + (plotW * i) / 4, 2);
  }
  ctx.textAlign = 'left';
  canvas.dataset.rows = String(channels.length);
  canvas.dataset.drawn = String(channels.reduce((n, c) => n + c.rows.length, 0));
}

function pointer(ev) {
  const canvas = document.getElementById('chHistCanvas');
  const rect = canvas.getBoundingClientRect();
  const x = ((ev.clientX - rect.left) * canvas.width) / rect.width;
  const y = ((ev.clientY - rect.top) * canvas.height) / rect.height;
  const hit = layout.find((l) => y >= l.y && y < l.y + ROW_H);
  return { x, hit };
}

function hover(ev) {
  const out = document.getElementById('chHistHover');
  const { x, hit } = pointer(ev);
  if (!hit || !data) { out.textContent = ''; return; }
  const c = data.channels.find((ch) => ch.key === hit.key);
  const canvas = document.getElementById('chHistCanvas');
  const plotW = canvas.width - LABEL_W;
  const t = data.start + ((x - LABEL_W) / plotW) * (data.end - data.start);
  const r = c.rows.reduce((best, row) => (best === null || Math.abs(row[0] - t) < Math.abs(best[0] - t) ? row : best), null);
  if (!r || Math.abs(r[0] - t) > Math.max(data.gap, 60) * 1.5) { out.textContent = label(c); return; }
  const snr = r[4] === null ? '-' : `${r[4]} dB`;
  out.textContent = `${label(c)} at ${formatClock(r[0] * 1000, true)}: SNR ${snr}, power ${r[3]} dBmV, +${r[5]} uncorrectables`;
}

function select(ev) {
  const { hit } = pointer(ev);
  if (!hit || !data) return;
  selected = hit.key;
  drawHeatmap();
  drawDetail();
}

function drawDetail() {
  const box = document.getElementById('chHistDetailBox');
  const c = data && data.channels.find((ch) => ch.key === selected);
  if (!c) { box.hidden = true; return; }
  box.hidden = false;
  setText(document.getElementById('chHistDetailTitle'), `${c.dir === 'd' ? 'Downstream' : 'Upstream'} channel ${c.id}`);
  const pts = (i) => c.rows.map((r) => ({ x: r[0] * 1000, y: r[i] }));
  const datasets = [
    { label: 'SNR dB', data: pts(4), borderColor: series(0), backgroundColor: series(0), yAxisID: 'y', pointRadius: 0, borderWidth: 1.5 },
    { label: 'Power dBmV', data: pts(3), borderColor: series(1), backgroundColor: series(1), yAxisID: 'y', pointRadius: 0, borderWidth: 1.5 },
    { type: 'bar', label: 'Uncorrectables', data: pts(5), borderColor: token('--crit'), backgroundColor: token('--crit'), yAxisID: 'y1', barThickness: 2 }
  ];
  if (detailChart) detailChart.destroy();
  detailChart = new Chart(document.getElementById('chHistDetailChart'), {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      scales: {
        x: {
          type: 'linear', min: data.start * 1000, max: data.end * 1000, grid: {},
          ticks: { maxTicksLimit: 8, font: { size: 10 }, callback: (v) => formatClock(v, rangeSeconds > 86400) }
        },
        y: { grid: {}, ticks: { font: { size: 10 } }, title: { display: true, text: 'dB / dBmV', font: { size: 10 } } },
        y1: { position: 'right', beginAtZero: true, grid: { display: false }, ticks: { font: { size: 10 } }, title: { display: true, text: 'new errors', font: { size: 10 } } }
      },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 10, font: { size: 10 }, padding: 6 } },
        tooltip: { callbacks: { title: (items) => (items.length ? formatClock(items[0].parsed.x, true) : '') } }
      }
    }
  });
}

async function load() {
  const status = document.getElementById('chHistStatus');
  if (!deviceId) return;
  const seq = ++requestSeq;
  setText(status, 'Loading…');
  const end = Math.floor(Date.now() / 1000);
  const start = end - rangeSeconds;
  const q = new URLSearchParams({ start_time: String(start), end_time: String(end) });
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/channels/history?${q}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (seq !== requestSeq) return;
    data = groupChannels(body.rows, start, end);
    if (selected && !data.channels.some((c) => c.key === selected)) selected = null;
    drawHeatmap();
    drawDetail();
    setText(status, data.count ? `${data.count} readings, ${data.channels.length} channels` : 'No channel readings in this range.');
  } catch (e) {
    if (seq === requestSeq) setText(status, `Could not load channel history: ${e.message}`);
  }
}

function markButtons() {
  document.querySelectorAll('#chHistRanges button').forEach((b) => b.classList.toggle('active', Number(b.dataset.arg) === rangeSeconds));
  document.querySelectorAll('#chHistMetrics button').forEach((b) => b.classList.toggle('active', b.dataset.arg === metric));
}

export function setChannelDevice(id) {
  if (id !== deviceId) { data = null; selected = null; }
  deviceId = id;
  device = state.devices.find((d) => d.id === id) || null;
}

export function showChannelsTab(name) {
  const history = name === 'history';
  document.getElementById('channelsCurrentPane').hidden = history;
  document.getElementById('channelsHistoryPane').hidden = !history;
  document.querySelectorAll('#channelsTabs button').forEach((b) => b.classList.toggle('active', b.dataset.arg === name));
  if (history) {
    markButtons();
    load();
  }
}

export function setChannelHistoryRange(seconds) {
  if (!RANGES[seconds]) return;
  rangeSeconds = seconds;
  markButtons();
  load();
}

export function setChannelHistoryMetric(name) {
  if (name !== 'snr' && name !== 'power') return;
  metric = name;
  markButtons();
  drawHeatmap();
}

export function initChannelHistory() {
  const canvas = document.getElementById('chHistCanvas');
  canvas.addEventListener('mousemove', hover);
  canvas.addEventListener('click', select);
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  showChannelsTab: (el) => showChannelsTab(el.dataset.arg),
  setChannelHistoryRange: (el) => setChannelHistoryRange(Number(el.dataset.arg)),
  setChannelHistoryMetric: (el) => setChannelHistoryMetric(el.dataset.arg)
});
