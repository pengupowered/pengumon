// Hop details: one row per target (loss, latency range, last, jitter, totals, the last 60 probes), from the `targets`
// topic. model.js makes the view model, view.js is the standard view (docs/themes.md, "Drawing a card"). The columns have
// fixed widths (panel.css), so a figure that changes width (LOST, a large jitter) does not move the others.

import { mountMatrix } from './model.js';
import './help.js';

export default {
  id: 'matrix',
  title: 'Hop details',
  tab: 'live',
  icon: 'route',
  core: true,
  available: () => true,
  mount: (section) => mountMatrix(section)
};
