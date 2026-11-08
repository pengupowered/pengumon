// Speed panel: the scheduled speed test (config: speedtest). model.js polls GET /api/speedtests and makes the view model;
// view.js is the standard view (docs/themes.md, "Drawing a card").

import { initSpeed } from './model.js';

export default {
  id: 'speed',
  title: 'Speed',
  tab: 'network',
  icon: 'gauge',
  core: true,
  available: () => true,
  mount: () => initSpeed()
};
