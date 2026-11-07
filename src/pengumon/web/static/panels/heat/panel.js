// When it breaks: packet loss by day and hour over the last 7 days. model.js fetches and makes the view model, view.js is
// the standard view (docs/themes.md, "Drawing a card").

import { initHeat } from './model.js';

export default {
  id: 'heat',
  title: 'When it breaks',
  tab: 'incidents',
  icon: 'heat',
  core: true,
  available: () => true,
  mount: (section) => initHeat(section)
};
