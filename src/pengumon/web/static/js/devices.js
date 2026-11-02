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

// The reason a panel is missing, from the first of its features that says why (the text is for people; the logic
// uses reason_kind only).
export function missingNote(featureNames, what) {
  for (const name of featureNames) {
    const f = feature(name);
    if (f && !f.available) {
      return f.reason_kind === 'none' ? `${what}: no device configured (${f.reason})` : `${what}: ${f.reason}`;
    }
  }
  return `${what}: not available`;
}
