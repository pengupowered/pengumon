// The effective settings values, for the places that quote a threshold (the help texts, the latency panel's opening
// range): GET /api/settings/values, read once before the panels mount and again whenever the live message's
// `settings_revision` differs from the revision read (a Save here or in another browser). Nothing here edits a setting;
// the Settings dialog (dialogs/settings) does that.

import { bus } from './bus.js';

let values = {};
let revision = null;

// {key: effective value}; empty until the first read, so a text always has a fallback (`val` below).
export const settingsValues = () => values;

// One value, or `fallback` when the key is unknown (the read failed, or an older server).
export function val(v, key, fallback) {
  const x = v ? v[key] : undefined;
  return x === undefined || x === null ? fallback : x;
}

async function read() {
  try {
    const res = await fetch('/api/settings/values', { cache: 'no-store' });
    if (!res.ok) return false;
    const body = await res.json();
    values = body.values || {};
    revision = body.revision;
  } catch (e) {
    return false; // the texts keep their fallbacks
  }
  return true;
}

// One read at a time (the live message repeats the new revision every second until a read has it, and answers could
// otherwise land out of order). A request that comes while a read is under way (a Save, or a newer revision) may have
// changed what it reads, so it is read once more, at most twice.
let inflight = null;
let again = false;
export function refreshSettingsValues() {
  if (inflight) { again = true; return inflight; }
  inflight = (async () => {
    let ok;
    for (let n = 0; n < 3; n++) {
      again = false;
      ok = await read();
      if (!again || !ok) break;
    }
    return ok;
  })().finally(() => { inflight = null; });
  return inflight;
}

// The revision the page's settings were read at (<body data-settings-revision>), or null for a page rendered without one
// (an older server): then the first live message's revision is taken as the page's own.
export function renderedRevision() {
  const at = document.body.dataset.settingsRevision;
  return at !== undefined && /^\d+$/.test(at) ? Number(at) : null;
}

// The page carries the values it was rendered with (<body data-settings>, from web/pages.py) and the revision they were
// read at (data-settings-revision), so help texts need no fetch and a Save made between the render and the live
// connection shows as a first message with another revision, which reads them again. Without them (an older server) the
// first read is a request.
export async function initSettingsValues() {
  try {
    const rendered = JSON.parse(document.body.dataset.settings || '{}');
    if (rendered && Object.keys(rendered).length) {
      values = rendered;
      revision = renderedRevision();
      return true;
    }
  } catch (e) { /* fall through to the request */ }
  return refreshSettingsValues();
}

bus.subscribe('settings_revision', (rev) => {
  if (revision === null && Object.keys(values).length && typeof rev === 'number') { revision = rev; return; } // rendered without a revision: the first message's is taken as its own
  if (revision !== null && typeof rev === 'number' && rev !== revision) refreshSettingsValues();
});
