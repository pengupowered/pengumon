// The list editors by list name.

import devicesEditor from './devices.js';
import services from './services.js';
import webhooks from './webhooks.js';
import dns from './dns.js';

// The devices editor takes the server's messages as [{where, message}] (lists.devices[1].url or devices.<id>.<field>)
// and hands back the ones it could not place; the page passes the per-index map and then every message, so it gets
// the devices' ones from the second.
const devices = {
  ...devicesEditor,
  problems: (_perIndex, all) => devicesEditor.problems(Object.entries(all || {})
    .filter(([where]) => /^(lists\.devices\[|devices\.)/.test(where))
    .map(([where, message]) => ({ where, message }))),
  redraw() {}
};

export const LIST_EDITORS = { devices, services, webhooks, dns_probes: dns };
