// Latency and packet loss panel: the live charts and their 15 minute to 24 hour history views.

import { initCharts, mountCharts } from './charts.js';
import { loadHistoricalRange, preloadLiveHistory } from './history.js';

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
    // another panel asks to show a time window (the inspector's "Show in latency chart")
    ctx.bus.subscribe('chart:range', (r) => loadHistoricalRange(r.start, r.end));
  }
};
