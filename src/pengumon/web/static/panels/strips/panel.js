// Uptime: one strip per target in path order (GET /api/uptime). model.js fetches and makes the view model, view.js is the
// standard view (docs/themes.md, "Drawing a card").

import { initStrips } from './model.js';

export default {
  id: 'strips',
  title: 'Uptime',
  tab: 'live',
  icon: 'grid',
  core: true,
  available: () => true,
  mount: (section) => initStrips(section)
};
