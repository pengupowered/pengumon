// Reliability report panel: availability per day and hop, causes and worst hours (the dossier button in the footer shares
// its module). model.js fetches and makes the view model, view.js is the standard view (docs/themes.md, "Drawing a card").

import { initReport } from './model.js';

export default {
  id: 'report',
  title: 'Reliability',
  tab: 'incidents',
  icon: 'report',
  core: true,
  available: () => true,
  mount: (section) => initReport(section)
};
