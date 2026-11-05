// The configured devices and the features they enable, loaded once at startup (/api/devices, /api/features) into
// `state`. What the installation has decides which panels exist (panels/index.js, layout.js); nothing here knows any
// panel.

import { state } from './state.js';

async function fetchJson(url) {
  try {
    const res = await fetch(url);
    return res.ok ? await res.json() : null;
  } catch (e) {
    console.warn(`Failed to load ${url}:`, e);
    return null;
  }
}

export async function loadDevices() {
  const [devices, features] = await Promise.all([fetchJson('/api/devices'), fetchJson('/api/features')]);
  state.devicesKnown = Array.isArray(devices); // false: the list could not be read, so nobody says there are none
  state.devices = Array.isArray(devices) ? devices : [];
  state.features = Array.isArray(features) ? features : [];
}

// The device the single-device views use for a capability, the way the server picks it (DeviceRegistry.primary): the
// first enabled device offering it, else the first one offering it at all. Null when none does.
export function primaryDevice(capability) {
  const offering = state.devices.filter((d) => d.offers.includes(capability));
  return offering.find((d) => d.enabled) || offering[0] || null;
}

export function featureAvailable(name) {
  const f = feature(name);
  return !!f && f.available;
}

function feature(name) {
  return state.features.find(f => f.feature === name) || null;
}

// The reason a panel is missing, in plain words for the Layout dialog and the panels' own notes (the logic uses
// reason_kind only; the technical reason the server sends stays out of the page). `fix` is the panel's own sentence
// on where and what to add, appended as it is.
export function missingNote(featureNames, what, fix = '') {
  const WHY = { none: 'no device added yet.', disabled: 'its device is switched off.', not_configured: 'its device needs its password.' };
  for (const name of featureNames) {
    const f = feature(name);
    if (!f || f.available) continue;
    return `${what}: ${WHY[f.reason_kind] || 'not available.'}${fix ? ` ${fix}` : ''}`;
  }
  return `${what}: not available`;
}
