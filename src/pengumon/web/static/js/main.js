// Pengumon Real-time & Historical Web Frontend
//
// Starts the page: reads what the installation has (devices, features), mounts the panels the registry lists and the
// layout puts on the page, and connects the websocket. Panels start their own data and register their own actions;
// nothing here names one (the server finds the panel folders; panels/index.js loads what it found).

import './session.js'; // first: wraps fetch, so a refused sign-in sends the page to /login
import { initTargets } from './util.js';
import { featureAvailable, loadDevices, missingNote, primaryDevice } from './devices.js';
import { state } from './state.js';
import { bus } from './bus.js';
import { initHelp } from './help.js';
import { initTabs } from './tabs.js';
import { mountLayout } from './layout.js';
import { connectWebSocket } from './live.js';
import { initChrome } from './chrome.js';
import { loadModules } from './panels/index.js';

// What a panel gets: the bus and what the installation has (devices, features).
function context() {
  return {
    bus,
    devices: state.devices,
    features: state.features,
    featureAvailable,
    devicesOffering: (...capabilities) => state.devices.filter((d) => d.enabled && d.offers.some((c) => capabilities.includes(c))),
    reason: missingNote,
    primary: primaryDevice
  };
}

document.addEventListener('DOMContentLoaded', async () => {
  initChrome();
  await Promise.all([initTargets(), loadDevices()]);
  initHelp();
  mountLayout(await loadModules(), context());
  initTabs();
  connectWebSocket();
});
