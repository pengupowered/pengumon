// The effective settings values, for the places that quote a threshold (the help texts, the latency panel's opening
// range): GET /api/settings/values, read once before the panels mount and again whenever the live message's
// `settings_revision` differs from the revision read (a Save here or in another browser). Nothing here edits a setting;
// the Settings dialog (dialogs/settings) does that.

import { bus } from './bus.js';

let values = {};
let revision = null;
const listeners = new Set();

// {key: effective value}; empty until the first read, so a text always has a fallback (`val` below).
export const settingsValues = () => values;

// One value, or `fallback` when the key is unknown (the read failed, or an older server).
export function val(v, key, fallback) {
  const x = v ? v[key] : undefined;
  return x === undefined || x === null ? fallback : x;
}

export async function refreshSettingsValues() {
  try {
    const res = await fetch('/api/settings/values', { cache: 'no-store' });
    if (!res.ok) return false;
    const body = await res.json();
    values = body.values || {};
    revision = body.revision;
  } catch (e) {
    return false; // the texts keep their fallbacks
  }
  [...listeners].forEach((fn) => {
    try { fn(values); } catch (e) { console.error(`settings-values: a listener threw: ${e && e.message ? e.message : e}`); }
  });
  return true;
}

// fn(values) runs after the values changed. Returns the function that stops it.
export function onSettingsChanged(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// The page carries the values it was rendered with (<body data-settings>, from web/pages.py), so help texts need no fetch;
// without them (an older server) the first read is a request.
export async function initSettingsValues() {
  try {
    const rendered = JSON.parse(document.body.dataset.settings || '{}');
    if (rendered && Object.keys(rendered).length) { values = rendered; return true; }
  } catch (e) { /* fall through to the request */ }
  return refreshSettingsValues();
}

bus.subscribe('settings_revision', (rev) => {
  if (revision === null && Object.keys(values).length && typeof rev === 'number') { revision = rev; return; } // rendered by the server: the first message's revision is its own
  if (revision !== null && typeof rev === 'number' && rev !== revision) refreshSettingsValues();
});
