// Incident log panel: the table of outages, with the inspector that opens from a row.

import { updateIncidentsTable } from './incidents.js';
import './inspector.js';
import './help.js';

export default {
  id: 'incidents',
  title: 'Incidents',
  tab: 'incidents',
  icon: 'bell',
  core: true,
  available: () => true,
  mount(section, ctx) {
    // The list is fetched (gzipped) at start and whenever the server's incidents_version changes, which includes the
    // first message after a reconnect; the latency chart gets it too, for its incident windows.
    const show = (list) => { updateIncidentsTable(list); ctx.bus.publish('incidents:list', list); };
    const load = () => fetch('/api/incidents?limit=500')
      .then(r => r.json())
      .then(show)
      .catch(console.error);
    let version = null;
    ctx.bus.subscribe('incidents_version', (v) => {
      if (v === version) return;
      const first = version === null;
      version = v;
      if (!first) load();
    });
    ctx.bus.subscribe('connection', (state) => { if (state === 'open' && version !== null) load(); });
    load();
  }
};
