// Reliability report panel: availability per day and hop, causes and worst hours (the dossier button in the header
// shares its module).

import { followQuietHops, loadReport } from './report.js';

export default {
  id: 'report',
  title: 'Reliability',
  tab: 'incidents',
  icon: 'report',
  core: true,
  available: () => true,
  mount: () => { loadReport(7); followQuietHops(); }
};
