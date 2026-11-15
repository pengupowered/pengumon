// Latency history: the range buttons, the live backfill, the stored-history views and the incident window the inspector
// asks for. Each view fetches /api/history (compact rows), turns the per-target samples into the chart's view model
// (path series, band, loss, charts.js) and adds the WAN download from the primary WAN device's history.

import { registerActions } from '../../js/dispatch.js';
import { maxLivePoints, state } from '../../js/state.js';
import { localFormat } from '../../js/clock.js';
import { has, median } from '../../js/values.js';
import { pageLeaving } from '../../js/modal.js';
import { targetList } from '../../js/targets.js';
import { SERIES, combine, roundKeys, seriesMembers, wholeEdges } from '../../js/path.js';
import { joinLive, liveFeedReady, pingTargets, renderCharts, resetView, setLineMode, view } from './charts.js';
import { settingsValues, val } from '../../js/settings-values.js';
import { primaryDevice } from '../../js/devices.js';
import { applyTabs, selectTab } from '../../js/tabs.js';
import { allStoredWords, fillRangeSelect, loadFromOldest, longBucket, onRetentionChange, rangeOptions, retentionDays, stepWords } from '../../js/ranges.js';

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

// Samples (one per target and probe or bucket) -> the view model. `round`: raw samples, grouped by probe round (roundKeys),
// a point per round as the live feed draws a point per message; else the server's buckets, a point each.
function fill(samples, round) {
  const members = seriesMembers(targetList());
  const byTime = new Map();
  const rounds = round ? roundKeys(samples.map((s) => s.timestamp)) : null;
  samples.forEach((s) => {
    const ts = round ? rounds.get(s.timestamp) : s.timestamp;
    if (!byTime.has(ts)) byTime.set(ts, {});
    byTime.get(ts)[s.target_id] = s;
  });
  // (the newest and oldest second or bucket may hold some targets only: left out, see wholeEdges)
  const inWindow = new Set(samples.map((s) => s.target_id));
  const pathIds = [...new Set(Object.values(members).flat())].filter((id) => inWindow.has(id));
  const times = wholeEdges([...byTime.keys()].sort((a, b) => a - b), (ts) => byTime.get(ts), pathIds, round ? 5 : 2);
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
  pingTargets().forEach((t) => {
    if (inWindow.has(t.id)) view.byTarget[t.id] = times.map((ts) => lat(byTime.get(ts)[t.id]));
  });
  // loss: the worst path target at each point, and which one it was (the lane's tooltip names it)
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
    view.wan = view.times.map((t) => { // (the points the live feed added while this was fetched are in view.times too)
      while (j + 1 < snaps.length && Math.abs(snaps[j + 1].timestamp - t) <= Math.abs(snaps[j].timestamp - t)) j++;
      return Math.abs(snaps[j].timestamp - t) <= tolerance ? Math.round(snaps[j].wan_rx_rate_mbps * 10) / 10 : null;
    });
    renderCharts();
  } catch (err) {
    if (!pageLeaving()) console.error('Error loading WAN load overlay:', err); // (a reload cuts the request off: no error)
  }
}

// The range control: Live, 15 m and the stored ranges the probes' retention keeps (js/ranges.js). The chart's mode is
// 'live', 'custom' (an incident's window, which the list has no entry for) or the seconds of a range.
const options = () => rangeOptions({ days: retentionDays('probes'), min: '1h', live: true, quarter: true });
const INCIDENT_WINDOW = { id: 'custom', words: 'Incident window' };

export function markRange(mode = state.currentMode) {
  const host = document.getElementById('timeRangeButtons');
  if (!host) return;
  const list = options();
  const hit = mode === 'live' ? list.find((o) => o.id === 'live') : list.find((o) => o.seconds === mode);
  const value = mode === 'custom' ? INCIDENT_WINDOW.id : hit ? hit.id : list[0].id;
  fillRangeSelect(host, { options: list, value, action: 'setTimeRange', label: 'Time range', extra: INCIDENT_WINDOW });
}

const label = (text) => { document.getElementById('chartModeLabel').textContent = text; };
const span = (seconds) => (seconds >= 86400 * 2 ? `${Math.round(seconds / 86400)} d` : seconds >= 3600 ? `${seconds / 3600} h` : `${seconds / 60} min`);
const per = stepWords;
// Seconds per bucket of each range: 7 d and 30 d keep about 700 points a line (the server reads the coarser tiers); past 30 d
// the width is the span over about 720, in whole quarter hours (a stored tier), so every range draws 400-800 points.
export const bucketFor = (seconds) => (seconds > 2592000 ? longBucket(seconds) : seconds >= 2592000 ? 3600 : seconds >= 604800 ? 900 : seconds >= 86400 ? 60 : seconds >= 3600 ? 10 : 2);

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
  await liveFeedReady(3000); // (the window then holds every round before the feed's first message: no seam between them)
  if (seq !== requestSeq || state.currentMode !== 'live') return;
  const now = Date.now() / 1000;
  const startTime = now - maxLivePoints;
  try {
    const samples = await fetchSamples(`start_time=${startTime}&end_time=${now}`);
    if (seq !== requestSeq || state.currentMode !== 'live' || !samples || !samples.length) return;
    const times = fill(samples, true);
    // the feed's messages of the rounds after the newest stored one: a round's message goes out before the next round
    // starts, an interval after it began, so a message later than that is of a later round
    if (times.length) joinLive(times[times.length - 1] + (Number(val(settingsValues(), 'prober.interval_seconds', 1)) || 1));
    attachLoadOverlay(seq, times, startTime, now, 30);
  } catch (err) {
    if (!pageLeaving()) console.error('Error preloading live history:', err); // (a reload cuts the request off: no error)
  }
}

async function loadHistory(durationSeconds) {
  const seq = ++requestSeq;
  const now = Date.now() / 1000;
  const all = options().some((o) => o.all && o.seconds === durationSeconds);
  label(`${all ? 'all' : 'last'} ${span(durationSeconds)} · avg per ${per(bucketFor(durationSeconds))}`);
  try {
    // All starts at the oldest stored probe when that is younger than the retention (js/ranges.js): buckets suit that span
    const { answer: samples, start, bucket, since } = await loadFromOldest({
      all, start: now - durationSeconds, end: now, bucketOf: bucketFor, current: () => seq === requestSeq,
      ask: (from, to, width) => fetchSamples(`start_time=${from}&end_time=${to}&bucket_seconds=${width}`),
      oldest: (rows) => rows.reduce((m, r) => (m === null || r.timestamp < m ? r.timestamp : m), null)
    });
    if (seq !== requestSeq) return;
    if (all) label(`${allStoredWords(retentionDays('probes'), since)} · avg per ${per(bucket)}`);
    if (!samples || !samples.length) { emptied(samples ? 'no probes stored in this range' : 'could not load the history'); return; }
    const times = fill(samples, false);
    attachLoadOverlay(seq, times, start, now, Math.max(30, bucket * 1.5));
  } catch (err) {
    if (pageLeaving()) return; // (a reload cuts the request off: no error)
    console.error('Error fetching history:', err);
    if (seq === requestSeq) emptied('could not load the history');
  }
}

// A range that cannot be drawn: the chart is emptied (it must not go on showing the previous range under this one's
// name) and the label says why.
function emptied(reason) {
  resetView();
  renderCharts();
  label(`${document.getElementById('chartModeLabel').textContent} · ${reason}`);
}

// On a phone the chart sits on the Live tab, which is not shown while the Incidents tab is: open it, or the window is
// drawn where nobody sees it.
function showLatencyTab() {
  const section = document.querySelector('[data-panel="latency"]');
  if (!section || !section.hidden) return;
  selectTab((section.dataset.tab || '').split(/\s+/)[0]);
  applyTabs();
}

export async function loadHistoricalRange(startTime, endTime) {
  const seq = ++requestSeq;
  state.currentMode = 'custom';
  const t = (s) => localFormat(s * 1000, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  label(`incident window ${t(startTime)} – ${t(endTime)}`);
  markRange('custom');
  showLatencyTab();
  try {
    const samples = await fetchSamples(`start_time=${startTime}&end_time=${endTime}`);
    if (seq !== requestSeq || !samples || !samples.length) return;
    const times = fill(samples, true);
    attachLoadOverlay(seq, times, startTime, endTime, 30);
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.querySelector('[data-panel="latency"] .panel').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth' });
  } catch (err) {
    if (!pageLeaving()) console.error('Error loading custom range:', err); // (a reload cuts the request off: no error)
  }
}

// The actions this module's markup names (see dispatch.js).
registerActions('change', {
  setTimeRange: (el) => {
    const hit = options().find((o) => o.id === el.value);
    if (hit) setTimeRange(hit.id === 'live' ? 'live' : hit.seconds);
  }
});
onRetentionChange(() => markRange());
registerActions('click', {
  setLatencyView: (el) => setLineMode(el.dataset.arg)
});
