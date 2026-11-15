// Router history modal: CPU / memory, WAN rates (with the line capacity when known) and client count over 1 h up to All (the retention),
// from the server-side buckets of GET /api/devices/{id}/history?max_points=. Fetched on open and on a range change only.

import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { deviceName } from '../../js/device-text.js';
import { formatClock, zoneOffsetMs } from '../../js/clock.js';
import { keepKeyboard, openModal } from '../../js/modal.js';
import { setText } from '../../js/dom.js';
import { series as tone, token } from '../../js/chart-theme.js';
import { allStoredWords, axisLabel, distinctTicks, fillRangeSelect, loadFromOldest, monthTicks, monthsPast, rangeOptions, retentionDays, stepWords } from '../../js/ranges.js';

// The ranges on offer: 1 h up to what the router's history is kept for (js/ranges.js).
const options = () => rangeOptions({ days: retentionDays('router'), min: '1h' });
const MAX_POINTS = 720; // (about 400-800 points a line from 24 h on)
const Y_WIDTH = 54; // every chart gets the same y-axis width, so the three plot areas line up on the shared x axis
const charts = {};
let rangeSeconds = 3600;
const rangeName = () => { const o = options().find((x) => x.seconds === rangeSeconds); return o ? o.words.toLowerCase() : `last ${Math.round(rangeSeconds / 86400)} days`; };
let device = null; // the router whose history is open
let requestSeq = 0;
let lastBody = null; // the last answer drawn, for a redraw after a theme change
let times = []; // the bucket times (ms) of the loaded answer: the crosshair snaps to them
let bucketSeconds = 60;
let drawnSeconds = 3600; // the span the charts draw (All starts at the oldest stored row): the axis labels follow it, not the range's name
let hoverIdx = null; // the bucket the crosshair is on, in every chart
const readers = {}; // chart id -> (row index) => readout text

const fmtMbps = (v) => (v === null || v === undefined ? '-' : v >= 100 ? String(Math.round(v)) : (Math.round(v * 10) / 10).toString());
const fmtPct = (v) => (v === null || v === undefined ? '-' : `${Math.round(v)}%`);
const fmtNum = (v) => (v === null || v === undefined ? '-' : String(Math.round(v * 10) / 10));

// The crosshair: a vertical line on every chart at the same bucket, a dot on each solid series, and a text readout in
// each section's heading (instead of per-chart tooltips). Moving over any chart, or the arrow keys on a focused one,
// move all of them. Also draws the capacity labels.
const crosshair = {
  id: 'rhCrosshair',
  afterEvent(chart, args) {
    const e = args.event;
    if (e.type === 'mouseout') return setHover(null);
    if (e.type !== 'mousemove' && e.type !== 'touchmove' && e.type !== 'touchstart') return undefined;
    const a = chart.chartArea;
    if (e.x < a.left || e.x > a.right || e.y < a.top || e.y > a.bottom) return setHover(null);
    return setHover(nearest(chart.scales.x.getValueForPixel(e.x)));
  },
  afterDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales } = chart;
    ctx.save();
    ctx.font = `10px ${token('--font')}`;
    ctx.fillStyle = token('--ink-2');
    chart.data.datasets.forEach((ds) => {
      if (ds.capLabel) {
        ctx.fillText(ds.capLabel, a.left + 4, scales.y.getPixelForValue(ds.data[0].y) - 5);
      }
    });
    if (hoverIdx !== null && times[hoverIdx] !== undefined) {
      const x = scales.x.getPixelForValue(times[hoverIdx]);
      ctx.strokeStyle = token('--ink-3');
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, a.top);
      ctx.lineTo(x, a.bottom);
      ctx.stroke();
      chart.data.datasets.forEach((ds) => {
        const p = ds.dot && (ds.aligned || ds.data)[hoverIdx];
        if (!p || p.y === null || p.y === undefined) return;
        ctx.beginPath();
        ctx.arc(x, scales.y.getPixelForValue(p.y), 3.5, 0, Math.PI * 2);
        ctx.fillStyle = ds.borderColor;
        ctx.fill();
        ctx.strokeStyle = token('--surface-1');
        ctx.lineWidth = 2;
        ctx.stroke();
      });
    }
    ctx.restore();
  }
};

function nearest(ms) {
  if (!times.length) return null;
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < ms) lo = mid + 1; else hi = mid;
  }
  return lo > 0 && ms - times[lo - 1] < times[lo] - ms ? lo - 1 : lo;
}

// The time of a bucket: with seconds while buckets are shorter than a minute, with the date over a day.
const stamp = (ms) => (bucketSeconds < 60 ? formatClock(ms, false, true)
  : `${formatClock(ms, drawnSeconds > 86400)}${bucketSeconds >= 3600 ? ` · ${stepWords(bucketSeconds)}` : ''}`); // (a long range's bucket is named)

function setHover(idx) {
  if (idx === hoverIdx) return;
  hoverIdx = idx;
  for (const [id, read] of Object.entries(readers)) {
    const el = document.getElementById(id.replace('Chart', 'Read'));
    if (el) el.textContent = idx === null || !charts[id] ? '' : `${stamp(times[idx])}  ·  ${read(idx)}`;
  }
  Object.values(charts).forEach((c) => c.draw());
}

function onKey(ev) {
  if (!times.length) return;
  const last = times.length - 1;
  const step = { ArrowLeft: -1, ArrowRight: 1 }[ev.key];
  if (step !== undefined) setHover(Math.max(0, Math.min(last, (hoverIdx === null ? last : hoverIdx) + step)));
  else if (ev.key === 'Home') setHover(0);
  else if (ev.key === 'End') setHover(last);
  else if (ev.key === 'Escape' && hoverIdx !== null) { setHover(null); ev.stopPropagation(); } else return;
  ev.preventDefault();
}

// Ticks on round local times (every 10 min, 1 h, 6 h, ... whichever gives at most 7), the same on all three charts.
const STEPS_MIN = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 5760, 10080, 20160];
function niceTicks(scale) {
  const { min, max } = scale;
  if (monthsPast(drawnSeconds)) { scale.ticks = monthTicks(min, max).map((value) => ({ value })); return; } // months past 90 days
  const step = (STEPS_MIN.find((m) => (max - min) / (m * 60000) <= 7) || 20160) * 60000;
  const off = -zoneOffsetMs(min); // the page's clock is UTC minus this
  const ticks = [];
  for (let t = Math.ceil((min - off) / step) * step + off; t <= max; t += step) ticks.push({ value: t });
  scale.ticks = ticks;
}

function lineChart(id, datasets, yTitle, yMax, fromZero = true) {
  const canvas = document.getElementById(id);
  if (charts[id]) charts[id].destroy();
  if (!canvas.dataset.kb) {
    canvas.dataset.kb = '1';
    canvas.addEventListener('keydown', onKey);
    canvas.addEventListener('blur', () => setHover(null));
  }
  charts[id] = new Chart(canvas, {
    type: 'line',
    data: { datasets },
    plugins: [crosshair],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      events: ['mousemove', 'mouseout', 'touchstart', 'touchmove'],
      layout: { padding: { right: 8, top: 6 } },
      scales: {
        x: {
          type: 'linear',
          grid: { display: false },
          afterBuildTicks: niceTicks,
          ticks: { font: { size: 10 }, maxRotation: 0, callback: distinctTicks((v) => axisLabel(v, drawnSeconds)) }
        },
        y: {
          beginAtZero: fromZero,
          suggestedMax: yMax,
          afterFit: (scale) => { scale.width = Y_WIDTH; },
          title: { display: false },
          grid: {},
          ticks: { font: { size: 10 }, maxTicksLimit: 5, callback: (v) => `${v}${yTitle === '%' ? ' %' : yTitle === '°C' ? ' °C' : ''}` }
        }
      },
      plugins: { legend: { display: false }, tooltip: { enabled: false } }
    }
  });
  return charts[id];
}

const series = (label, color, points, extra = {}) => ({
  label, data: points, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, tension: 0, dot: true, ...extra
});
const peak = (label, color, points) => series(label, color, points, { borderDash: [3, 3], borderWidth: 1, backgroundColor: 'transparent', dot: false });

function column(rows, fields, name) {
  const i = fields.indexOf(name);
  return rows.map((r) => ({ x: r[0] * 1000, y: r[i] }));
}

function legend(id, entries) {
  const box = document.getElementById(id);
  box.replaceChildren();
  entries.forEach(([name, color]) => {
    const s = box.appendChild(document.createElement('span'));
    const key = s.appendChild(document.createElement('i'));
    key.className = 'key line';
    key.style.setProperty('--c', color);
    s.append(name);
  });
}

export function renderRouterHistory(body) {
  lastBody = body;
  const { fields, rows, capacity } = body;
  const xMin = body.start_time * 1000;
  const xMax = body.end_time * 1000;
  const col = (name) => column(rows, fields, name);
  const showSystem = !!device && device.offers.includes('router_system');
  const showWan = !!device && device.offers.includes('wan_counters');
  document.getElementById('routerHistSystemBox').style.display = showSystem ? '' : 'none';
  document.getElementById('routerHistClientsBox').style.display = showSystem ? '' : 'none';
  document.getElementById('routerHistWanBox').style.display = showWan ? '' : 'none';
  times = rows.map((r) => r[0] * 1000);
  bucketSeconds = body.bucket_seconds || 60;
  drawnSeconds = Math.max(1, xMax - xMin) / 1000;
  hoverIdx = null;
  Object.keys(readers).forEach((k) => delete readers[k]);
  Object.keys(charts).forEach((id) => { charts[id].destroy(); delete charts[id]; }); // a device without a section keeps no chart
  document.querySelectorAll('.rh-read').forEach((el) => { el.textContent = ''; });
  const at = (name) => (i) => rows[i][fields.indexOf(name)];

  if (showSystem) {
    const cpu = tone(0);
    const mem = tone(1);
    lineChart('routerHistSystemChart', [
      series('CPU avg %', cpu, col('cpu_avg')), peak('CPU max %', cpu, col('cpu_max')),
      series('Memory avg %', mem, col('mem_avg')), peak('Memory max %', mem, col('mem_max'))
    ], '%', 100);
    legend('routerHistSystemLegend', [['CPU', 'var(--s1)'], ['Memory', 'var(--s2)']]);
    readers.routerHistSystemChart = (i) => `CPU ${fmtPct(at('cpu_avg')(i))} (peak ${fmtPct(at('cpu_max')(i))})  ·  Memory ${fmtPct(at('mem_avg')(i))} (peak ${fmtPct(at('mem_max')(i))})`;
    const clients = tone(2);
    lineChart('routerHistClientsChart', [
      series('Clients avg', clients, col('clients_avg'), { fill: true, backgroundColor: `${clients}33` }), peak('Clients max', clients, col('clients_max'))
    ], 'clients', 5);
    legend('routerHistClientsLegend', [['Clients', 'var(--s3)']]);
    readers.routerHistClientsChart = (i) => `Clients ${fmtNum(at('clients_avg')(i))} (peak ${fmtNum(at('clients_max')(i))})`;
  }
  if (showWan) {
    const down = tone(0);
    const up = tone(1);
    const sets = [
      series('Download avg', down, col('rx_avg'), { fill: true, backgroundColor: `${down}22` }), peak('Download max', down, col('rx_max')),
      series('Upload avg', up, col('tx_avg')), peak('Upload max', up, col('tx_max'))
    ];
    const caps = [];
    for (const [label, mbps, color] of [['download capacity', capacity && capacity.down_mbps, token('--ink-3')],
                                        ['upload capacity', capacity && capacity.up_mbps, token('--ink-3')]]) {
      if (mbps) {
        caps.push(`${mbps}`);
        sets.push(series(label, color, [{ x: xMin, y: mbps }, { x: xMax, y: mbps }],
          { borderDash: [8, 4], borderWidth: 1, backgroundColor: 'transparent', dot: false, capLabel: `${label} ${mbps} Mbps` }));
      }
    }
    lineChart('routerHistWanChart', sets, 'Mbps', capacity && capacity.down_mbps ? capacity.down_mbps * 1.05 : undefined);
    document.getElementById('routerHistWanSub').textContent = caps.length ? `Mbps, against line capacity ${caps.join(' / ')} Mbps` : 'Mbps (line capacity not known yet)';
    legend('routerHistWanLegend', [['Download', 'var(--s1)'], ['Upload', 'var(--s2)']]);
    readers.routerHistWanChart = (i) => `Download ${fmtMbps(at('rx_avg')(i))} (peak ${fmtMbps(at('rx_max')(i))})  ·  Upload ${fmtMbps(at('tx_avg')(i))} (peak ${fmtMbps(at('tx_max')(i))}) Mbps`;
  }
  // The CPU temperature, when the router reports one (the section is left out otherwise, and for a device without a reading in the range)
  const hasTemp = showSystem && fields.includes('temp_avg') && rows.some((r) => r[fields.indexOf('temp_avg')] !== null && r[fields.indexOf('temp_avg')] !== undefined);
  document.getElementById('routerHistTempBox').style.display = hasTemp ? '' : 'none';
  if (hasTemp) {
    const temp = tone(3);
    // the router is asked for its temperature about once a minute: in the 10-second slices of the last hour most slices hold
    // none, so the line joins readings up to 3 minutes or 1.5 slices apart, whichever is longer (a longer silence still
    // shows as a gap; past a day a slice is 15 minutes or more)
    // (Chart.js draws a null as a break whatever spanGaps says: spanGaps joins points that are in the data, so the slices
    // without a reading are left out of the drawn data; `aligned` keeps every slice for the crosshair, which reads by bucket)
    const joined = (d) => Object.assign(d, { aligned: d.data, data: d.data.filter((p) => p.y !== null && p.y !== undefined), spanGaps: Math.max(180000, 1500 * bucketSeconds) });
    lineChart('routerHistTempChart', [joined(series('Temperature avg', temp, col('temp_avg'))), joined(peak('Temperature max', temp, col('temp_max')))], '°C', undefined, false);
    legend('routerHistTempLegend', [['CPU temperature', 'var(--s4)']]);
    readers.routerHistTempChart = (i) => `${fmtNum(at('temp_avg')(i))} °C (peak ${fmtNum(at('temp_max')(i))} °C)`;
  }
  // One time axis for all the charts.
  for (const chart of Object.values(charts)) {
    chart.options.scales.x.min = xMin;
    chart.options.scales.x.max = xMax;
    chart.update();
  }
}

async function load() {
  const status = document.getElementById('routerHistStatus');
  if (!device) return;
  const seq = ++requestSeq;
  setText(status, 'Loading…');
  const end = Date.now() / 1000;
  const all = !!options().find((o) => o.seconds === rangeSeconds && o.all);
  try {
    const ask = async (from, to) => {
      const q = new URLSearchParams({ start_time: String(from), end_time: String(to), max_points: String(MAX_POINTS) });
      const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/history?${q}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    };
    // All starts at the oldest stored row when that is younger than the retention (js/ranges.js)
    const { answer: body, since } = await loadFromOldest({
      all, start: end - rangeSeconds, end, ask, current: () => seq === requestSeq,
      widthOf: (b) => b.bucket_seconds, oldest: (b) => (b.rows.length ? b.rows[0][0] : null)
    });
    if (seq !== requestSeq) return; // a newer range was chosen meanwhile
    renderRouterHistory(body);
    document.getElementById('routerHistModal').dataset.points = String(body.rows.length);
    const slice = stepWords(body.bucket_seconds);
    const what = all ? allStoredWords(retentionDays('router'), since) : rangeName();
    setText(status, body.rows.length ? `${what} · averages per ${slice}, dashed line: peak`
      : 'No router samples in this range.');
  } catch (e) {
    if (seq === requestSeq) setText(status, `Could not load router history: ${e.message}`);
  }
}

function markRange() {
  const host = document.getElementById('routerHistRanges');
  if (!host) return;
  const list = options();
  const hit = list.find((o) => o.seconds === rangeSeconds) || list.find((o) => o.id === '1h') || list[0];
  rangeSeconds = hit.seconds; // (a range the retention no longer keeps gives way to 1 h)
  fillRangeSelect(host, { options: list, value: hit.id, action: 'setRouterHistoryRange', label: 'Time range' });
}

export function openRouterHistory(deviceId) {
  device = state.devices.find((d) => d.id === deviceId) || null;
  if (!device) return;
  document.getElementById('routerHistTitle').textContent = `${deviceName(device)} history`;
  markRange();
  keepKeyboard(document.getElementById('routerHistModal'));
  openModal('routerHistModal');
  load();
}

export function setRouterHistoryRange(seconds) {
  if (!options().some((o) => o.seconds === seconds)) return;
  rangeSeconds = seconds;
  markRange();
  load();
}

// Chart.js keeps the colors it resolved when a chart was made, so a theme change redraws the open charts from scratch.
document.addEventListener('themechange', () => { if (lastBody && device) renderRouterHistory(lastBody); });

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openRouterHistory: (el) => openRouterHistory(el.dataset.arg)
});
registerActions('change', {
  setRouterHistoryRange: (el) => { const hit = options().find((o) => o.id === el.value); if (hit) setRouterHistoryRange(hit.seconds); }
});
