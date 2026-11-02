// Latency history: the range buttons, the live backfill, the stored-history views and the incident window the inspector
// asks for. Each view fetches /api/history (compact rows), turns the per-target samples into the chart's view model
// (path series, band, loss, charts.js) and adds the WAN download from the primary WAN device's history.

import { registerActions } from '../../js/dispatch.js';
import { maxLivePoints, state } from '../../js/state.js';
import { targetList } from '../../js/util.js';
import { SERIES, combine, median, seriesMembers } from '../../js/path.js';
import { pingTargets, renderCharts, resetView, setLineMode, view } from './charts.js';
import { primaryDevice } from '../../js/devices.js';

const has = (v) => v !== null && v !== undefined;
let requestSeq = 0;

// Fetches /api/history in its compact form (field names once, one array per row). Null when the request fails.
async function fetchSamples(query) {
  const res = await fetch(`/api/history?${query}&compact=true`);
  if (!res.ok) return null;
  const { fields, rows } = await res.json();
  return rows.map((row) => {
    const s = {};
    for (let i = 0; i < fields.length; i++) s[fields[i]] = row[i];
    return s;
  });
}

// Samples (one per target and second or bucket) -> the view model. `round`: group raw samples by whole seconds.
function fill(samples, round) {
  const members = seriesMembers(targetList());
  const byTime = new Map();
  samples.forEach((s) => {
    const ts = round ? Math.round(s.timestamp) : s.timestamp;
    if (!byTime.has(ts)) byTime.set(ts, {});
    byTime.get(ts)[s.target_id] = s;
  });
  const times = [...byTime.keys()].sort((a, b) => a - b);
  resetView();
  view.times = times;
  const lat = (s) => (s && (s.success === undefined || s.success) && has(s.latency_ms) ? s.latency_ms : null);
  SERIES.forEach((serie) => {
    const ids = members[serie.id];
    if (!ids) return;
    view.series[serie.id] = times.map((ts) => {
      const row = byTime.get(ts);
      const present = ids.filter((id) => row[id]);
      return present.length ? combine(serie, present.map((id) => lat(row[id]))) : null;
    });
    if (serie.id === 'path_internet') {
      // the band, when the server sends one (bucketed history: latency_p10_ms / latency_p90_ms)
      const band = (key) => times.map((ts) => median(ids.map((id) => byTime.get(ts)[id]?.[key])));
      view.lo = band('latency_p10_ms');
      view.hi = band('latency_p90_ms');
    }
  });
  if (!view.lo.length) { view.lo = times.map(() => null); view.hi = times.map(() => null); }
  // every pinged target on its own, for the "Each target" lines and the members in the path series' tooltips
  const present = new Set(samples.map((s) => s.target_id));
  pingTargets().forEach((t) => {
    if (present.has(t.id)) view.byTarget[t.id] = times.map((ts) => lat(byTime.get(ts)[t.id]));
  });
  // loss: the worst path target at each point, and which one it was (the lane's tooltip names it)
  const pathIds = Object.values(members).flat();
  view.loss = [];
  view.lossBy = [];
  times.forEach((ts) => {
    const row = byTime.get(ts);
    let worst = 0;
    let by = null;
    pathIds.filter((id) => row[id]).forEach((id) => {
      const pct = row[id].packet_loss_pct || (row[id].success === 0 ? 100 : 0);
      if (pct > worst) { worst = pct; by = id; }
    });
    view.loss.push(worst);
    view.lossBy.push(by);
  });
  view.wan = times.map(() => null);
  renderCharts();
  return times;
}

// The WAN download behind the latency: each point takes the nearest router reading within `tolerance` seconds.
async function attachLoadOverlay(seq, times, startTime, endTime, tolerance) {
  const device = primaryDevice('wan_counters');
  if (!times.length || !device) return;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/history?start_time=${startTime}&end_time=${endTime}`);
    if (!res.ok) return;
    const snaps = (await res.json()).filter((r) => has(r.wan_rx_rate_mbps));
    if (!snaps.length || seq !== requestSeq) return;
    let j = 0;
    view.wan = times.map((t) => {
      while (j + 1 < snaps.length && Math.abs(snaps[j + 1].timestamp - t) <= Math.abs(snaps[j].timestamp - t)) j++;
      return Math.abs(snaps[j].timestamp - t) <= tolerance ? Math.round(snaps[j].wan_rx_rate_mbps * 10) / 10 : null;
    });
    renderCharts();
  } catch (err) {
    console.error('Error loading WAN load overlay:', err);
  }
}

function markRange(mode) {
  document.querySelectorAll('#timeRangeButtons button').forEach((b) => {
    const on = b.dataset.arg === String(mode);
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

const label = (text) => { document.getElementById('chartModeLabel').textContent = text; };
const span = (seconds) => (seconds >= 3600 ? `${seconds / 3600} h` : `${seconds / 60} min`);

export function setTimeRange(mode) {
  state.currentMode = mode;
  markRange(mode);
  if (mode === 'live') {
    label('last 60 s · every probe');
    preloadLiveHistory();
  } else {
    loadHistory(mode);
  }
}

export async function preloadLiveHistory() {
  const seq = ++requestSeq;
  const now = Date.now() / 1000;
  const startTime = now - maxLivePoints;
  try {
    const samples = await fetchSamples(`start_time=${startTime}&end_time=${now}`);
    if (seq !== requestSeq || state.currentMode !== 'live' || !samples || !samples.length) return;
    const times = fill(samples, true);
    attachLoadOverlay(seq, times, startTime, now, 30);
  } catch (err) {
    console.error('Error preloading live history:', err);
  }
}

async function loadHistory(durationSeconds) {
  const seq = ++requestSeq;
  const now = Date.now() / 1000;
  const startTime = now - durationSeconds;
  const bucketSeconds = durationSeconds >= 86400 ? 60 : (durationSeconds >= 3600 ? 10 : 2);
  label(`last ${span(durationSeconds)} · avg per ${bucketSeconds} s`);
  try {
    const samples = await fetchSamples(`start_time=${startTime}&end_time=${now}&bucket_seconds=${bucketSeconds}`);
    if (seq !== requestSeq || !samples || !samples.length) return;
    const times = fill(samples, false);
    attachLoadOverlay(seq, times, startTime, now, Math.max(30, bucketSeconds * 1.5));
  } catch (err) {
    console.error('Error fetching history:', err);
  }
}

export async function loadHistoricalRange(startTime, endTime) {
  const seq = ++requestSeq;
  state.currentMode = 'custom';
  const t = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  label(`incident window ${t(startTime)} – ${t(endTime)}`);
  markRange('');
  try {
    const samples = await fetchSamples(`start_time=${startTime}&end_time=${endTime}`);
    if (seq !== requestSeq || !samples || !samples.length) return;
    const times = fill(samples, true);
    attachLoadOverlay(seq, times, startTime, endTime, 30);
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.getElementById('latencyChart').closest('.panel').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth' });
  } catch (err) {
    console.error('Error loading custom range:', err);
  }
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  setTimeRange: (el) => setTimeRange(el.dataset.arg === 'live' ? 'live' : Number(el.dataset.arg)),
  setLatencyView: (el) => setLineMode(el.dataset.arg)
});
