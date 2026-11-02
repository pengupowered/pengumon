// Latency and packet loss panel: the live charts and their 15 minute to 24 hour history views.

import { initCharts, mountCharts } from './charts.js';
import { loadHistoricalRange, preloadLiveHistory } from './history.js';
import { state } from '../../js/state.js';

// "1 ms resolution" next to the chart's mode when the ICMP backend times round trips in whole milliseconds (Windows,
// /api/info `probe.resolution_ms`, read once by chrome.js); with kernel timestamps (Linux, macOS) nothing is added.
function showProbeResolution(section, info) {
  const ms = info && info.probe && typeof info.probe.resolution_ms === 'number' ? info.probe.resolution_ms : 0;
  const note = section.querySelector('#probeResolution');
  if (!note || !(ms >= 1)) return;
  note.querySelector('#probeResolutionText').textContent = `${ms} ms resolution`;
  note.hidden = false;
}

export default {
  id: 'latency',
  title: 'Latency',
  tab: 'live',
  icon: 'activity',
  core: true,
  available: () => true,
  mount(section, ctx) {
    initCharts();
    mountCharts(ctx.bus);
    preloadLiveHistory();
    if (state.networkInfo) showProbeResolution(section, state.networkInfo);
    else document.addEventListener('networkinfo', (e) => showProbeResolution(section, e.detail), { once: true });
    // another panel asks to show a time window (the inspector's "Show in latency chart")
    ctx.bus.subscribe('chart:range', (r) => loadHistoricalRange(r.start, r.end));
  }
};
