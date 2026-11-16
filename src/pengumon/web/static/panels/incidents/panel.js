// Incident log panel: the timeline and the table of outages (model.js makes the view model, view.js is the standard view;
// docs/themes.md, "Drawing a card"), with the inspector that opens from a row (a dialog, not a card).

import { renderedRevision } from '../../js/settings-values.js';
import { initIncidentsCard, loadStart, reloadSpikes, updateIncidentsTable } from './model.js';
import { bindIncidents, loadSince, reload } from './list.js';
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
    initIncidentsCard(section);
    // The newest page is fetched at start and whatever is loaded so far whenever the server's incidents_version changes
    // (which includes the first message after a reconnect); older incidents are loaded when something needs them
    // (list.js). The latency chart gets the list too, for its incident windows.
    bindIncidents((list) => { updateIncidentsTable(list); ctx.bus.publish('incidents:list', list); });
    // the list reaches back as far as the timeline shows, in one request (and one for the outages); a chart range asked for from the inspector may be older
    const load = () => { reload(loadStart()); reloadSpikes(); }; // (the spikes: GET /api/latency-spikes, a new one bumps incidents_version)
    let version = null;
    ctx.bus.subscribe('incidents_version', (v) => {
      if (v === version) return;
      const first = version === null;
      version = v;
      if (!first) load();
    });
    // a Save in Settings (here or in another browser) may change how drops are grouped (incidents.group_gap_minutes): read again
    let revision = renderedRevision(); // the page's own, so a Save made between the render and the connection shows as a change
    ctx.bus.subscribe('settings_revision', (r) => {
      if (revision !== null && r !== revision) load();
      revision = r;
    }, { every: true });
    ctx.bus.subscribe('settings:saved', () => load()); // a Save made here (the dialog publishes it once the values are read again)
    ctx.bus.subscribe('connection', (state) => { if (state === 'open' && version !== null) load(); });
    load();
    ctx.bus.subscribe('chart:range', (r) => { if (r && typeof r.start === 'number') loadSince(r.start); });
  }
};
