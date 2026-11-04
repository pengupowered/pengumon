// Latency and packet loss panel: the live charts and their 15 minute to 24 hour history views.

import { initCharts, mountCharts } from './charts.js';
import { loadHistoricalRange, preloadLiveHistory, setTimeRange } from './history.js';
import { settingsValues, val } from '../../js/settings-values.js';
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

// display.default_latency_range (Settings > Display): the range the chart opens on, in seconds; 'live' is the 60 s live view.
const RANGE_SECONDS = { '15m': 900, '1h': 3600, '24h': 86400 };

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
    const opening = RANGE_SECONDS[val(settingsValues(), 'display.default_latency_range', 'live')];
    if (opening) setTimeRange(opening);
    else preloadLiveHistory();
    if (state.networkInfo) showProbeResolution(section, state.networkInfo);
    else document.addEventListener('networkinfo', (e) => showProbeResolution(section, e.detail), { once: true });
    // another panel asks to show a time window (the inspector's "Show in latency chart")
    // the tab was hidden: the live chart is built from the per-second messages, which were not delivered meanwhile
    // (live.js), so its last minute is fetched again (a replayed old value, from a panel mounted later, is ignored)
    ctx.bus.subscribe('tab:resumed', (r) => { if (r.missed && Date.now() - r.at < 5000 && state.currentMode === 'live') preloadLiveHistory(); });
    ctx.bus.subscribe('chart:range', (r) => loadHistoricalRange(r.start, r.end));
  }
};
