// Path health: the classifier's verdict and the path from this host to the internet, hop by hop, shown as the hero
// panel and as the header's status pill. model.js keeps the verdict, the path and the notices and makes the view model;
// view.js is the standard view (docs/themes.md, "Drawing a card").

import { mountDiagnosis } from './model.js';
import './help.js';

export default {
  id: 'diagnosis',
  title: 'Path health',
  tab: 'live',
  icon: 'path',
  core: true,
  available: () => true,
  mount: (section, ctx) => mountDiagnosis(ctx.bus, ctx)
};
