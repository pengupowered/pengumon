// Per-channel history of a DOCSIS line (the "History" tab of the channels modal): a heatmap of every channel over time
// on one canvas, colored by SNR (downstream) or power, and a line chart of the channel clicked. Data comes from
// GET /api/devices/{id}/channels/history, fetched when the tab opens and when the range changes.

import { registerActions } from '../../js/dispatch.js';
import { formatClock } from '../../js/clock.js';
import { setText } from '../../js/dom.js';
import { state } from '../../js/state.js';
import { gradeByRange } from '../../js/grades.js';
import { series, token } from '../../js/chart-theme.js';
import { allStoredWords, axisLabel, distinctTicks, fillRangeSelect, loadFromOldest, monthTicks, monthsPast, rangeOptions, retentionDays } from '../../js/ranges.js';

const ROW_H = 14;
const LABEL_W = 56;
const AXIS_H = 18;
// The ranges on offer: 24 h up to what the line's readings are kept for (js/ranges.js)
const options = () => rangeOptions({ days: retentionDays('line'), min: '24h' });
const DETAIL_POINTS = 720; // a channel's line chart draws about this many points whatever the range

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
function gradeOf(dir, metricName, row) {
  const [, , , power, snr] = row;
  const ranges = (device && device.ranges) || {};
  return metricName === 'snr' ? gradeByRange(ranges.ds_snr, snr)
    : gradeByRange(dir === 'd' ? ranges.ds_power : ranges.us_power, power);
}
const GRADE_RANK = { critical: 3, warn: 2, good: 1 };

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

// A time on the axis: the clock for a day, date and clock for a week, the date for a month, the month beyond 90 days.
// (by the span drawn: All starts at the oldest stored reading, so its labels are those of that span, not of the retention)
const drawn = () => (data ? Math.max(1, data.end - data.start) : rangeSeconds);
const axisTime = (ms) => axisLabel(ms, drawn());
const label = (c) => `${c.dir === 'd' ? 'DS' : 'US'} ${c.id}`;

function drawHeatmap() {
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
    // one cell per reading; where several land on one pixel column (a long range) the worst grade is the one drawn
    let lastX = null;
    let lastRank = -1;
    for (const r of c.rows) {
      const x = LABEL_W + Math.floor(((r[0] - data.start) / span) * plotW);
      const grade = gradeOf(c.dir, metric, r);
      const rank = GRADE_RANK[grade] || 0;
      if (x === lastX && rank <= lastRank) continue;
      lastX = x;
      lastRank = rank;
      ctx.fillStyle = grade ? token(GRADE_TOKEN[grade]) : none();
      ctx.fillRect(x, y, cellW, ROW_H - 1);
    }
    if (c.key === selected) {
      ctx.strokeStyle = token('--accent');
      ctx.strokeRect(LABEL_W - 0.5, y - 0.5, plotW + 1, ROW_H);
    }
  });
  ctx.fillStyle = token('--ink-3');
  ctx.textBaseline = 'top';
  let before = null;
  for (let i = 0; i <= 4; i++) {
    const t = data.start + (span * i) / 4;
    const text = axisTime(t * 1000);
    if (text === before) continue; // (no label twice in a row)
    before = text;
    ctx.textAlign = i === 0 ? 'left' : (i === 4 ? 'right' : 'center');
    ctx.fillText(text, LABEL_W + (plotW * i) / 4, 2);
  }
  ctx.textAlign = 'left';
  canvas.dataset.rows = String(channels.length);
  canvas.dataset.from = String(data.start); // (where the axis begins: epoch seconds)
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

// A channel's readings as at most DETAIL_POINTS points: the mean SNR and power of the readings in each slice of the window,
// and their new errors added up (a range of months holds tens of thousands of readings: a line chart of all would be a smear).
function thin(rows) {
  if (rows.length <= DETAIL_POINTS * 1.2) return rows;
  const width = (data.end - data.start) / DETAIL_POINTS;
  const slots = new Map();
  rows.forEach((r) => {
    const k = Math.floor((r[0] - data.start) / width);
    const a = slots.get(k) || { t: 0, n: 0, snr: 0, ns: 0, pow: 0, errs: 0 };
    a.t += r[0];
    a.n += 1;
    if (r[4] !== null && r[4] !== undefined) { a.snr += r[4]; a.ns += 1; }
    a.pow += r[3];
    a.errs += r[5] || 0;
    slots.set(k, a);
  });
  return [...slots.entries()].sort((p, q) => p[0] - q[0]).map(([, a]) => [Math.round(a.t / a.n), null, null,
    Math.round((a.pow / a.n) * 10) / 10, a.ns ? Math.round((a.snr / a.ns) * 10) / 10 : null, a.errs]);
}

function drawDetail() {
  const box = document.getElementById('chHistDetailBox');
  const c = data && data.channels.find((ch) => ch.key === selected);
  if (!c) { box.hidden = true; return; }
  box.hidden = false;
  setText(document.getElementById('chHistDetailTitle'), `${c.dir === 'd' ? 'Downstream' : 'Upstream'} channel ${c.id}`);
  const rows = thin(c.rows);
  const pts = (i) => rows.map((r) => ({ x: r[0] * 1000, y: r[i] }));
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
          afterBuildTicks(scale) { if (monthsPast(drawn())) scale.ticks = monthTicks(scale.min, scale.max).map((value) => ({ value })); },
          ticks: { maxTicksLimit: 8, font: { size: 10 }, callback: distinctTicks(axisTime) }
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
  const all = !!options().find((o) => o.seconds === rangeSeconds && o.all);
  // past 7 days the server keeps the newest reading of each bucket (the span over about 720, in whole quarter hours): 180 days of readings are 300,000 rows
  const bucketOf = (seconds) => (seconds > 604800 ? Math.ceil(seconds / 720 / 900) * 900 : 900);
  try {
    const ask = async (from, to, bucket) => {
      const q = new URLSearchParams({ start_time: String(from), end_time: String(to) });
      if (to - from > 604800) q.set('bucket_seconds', String(bucket));
      const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/channels/history?${q}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    };
    // All starts at the oldest stored reading when that is younger than the retention (js/ranges.js)
    const { answer: body, start, since } = await loadFromOldest({
      all, kind: 'line', start: end - rangeSeconds, end, bucketOf, ask, current: () => seq === requestSeq,
      oldest: (b) => b.rows.reduce((m, r) => (m === null || r[0] < m ? r[0] : m), null)
    });
    if (seq !== requestSeq) return;
    data = groupChannels(body.rows, start, end);
    data.since = since;
    if (selected && !data.channels.some((c) => c.key === selected)) selected = null;
    drawHeatmap();
    drawDetail();
    setText(status, data.count ? `${all ? `${allStoredWords(retentionDays('line'), since)}: ` : ''}${data.count} readings, ${data.channels.length} channels` : 'No channel readings in this range.');
  } catch (e) {
    if (seq === requestSeq) setText(status, `Could not load channel history: ${e.message}`);
  }
}

function markButtons() {
  const host = document.getElementById('chHistRanges');
  if (host) {
    const list = options();
    const hit = list.find((o) => o.seconds === rangeSeconds) || list[0];
    rangeSeconds = hit.seconds; // (a range the retention no longer keeps gives way to 24 h)
    fillRangeSelect(host, { options: list, value: hit.id, action: 'setChannelHistoryRange', label: 'Time range' });
  }
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

function setChannelHistoryRange(seconds) {
  if (!options().some((o) => o.seconds === seconds)) return;
  rangeSeconds = seconds;
  markButtons();
  load();
}

function setChannelHistoryMetric(name) {
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
  setChannelHistoryMetric: (el) => setChannelHistoryMetric(el.dataset.arg)
});
registerActions('change', {
  setChannelHistoryRange: (el) => { const hit = options().find((o) => o.id === el.value); if (hit) setChannelHistoryRange(hit.seconds); }
});
