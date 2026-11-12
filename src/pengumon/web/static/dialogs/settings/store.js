// What the Settings page knows: the schema, the server's state at the revision it loaded, and the draft, a change
// against that state. Nothing is sent anywhere from here; dialog.js saves the draft. The draft is
//   { values: {key: stored value | null (reset)}, lists: {name: items | null (reset)}, secrets: {slot: text | '' (remove)} }
// in the server's units (rounds, fractions, seconds); fields.js converts what is shown.

import { clone, same, sameItems } from './model.js';

const emptyDraft = () => ({ values: {}, lists: {}, secrets: {} });

export const S = {
  schema: null,
  spec: {}, // key -> setting spec
  server: null, // GET /api/settings
  draft: emptyDraft(),
  invalid: {}, // key -> message: what is typed in a box and not acceptable (the draft keeps the last acceptable value)
  errors: {}, // where -> message, from the last POST /api/settings/validate
  warnings: [], // [{where, message}] from the same answer
  summary: ''
};

const listeners = new Set();
// fn() runs after every edit of the draft (the Save label, the stale mark of a preview, the validation timer).
export const watchDraft = (fn) => { listeners.add(fn); };
export const changed = () => listeners.forEach((fn) => fn());

export const roundSeconds = () => (S.schema ? S.schema.round_seconds : 1);

export function load(schema, server) {
  S.schema = schema;
  S.spec = Object.fromEntries(schema.settings.map((s) => [s.key, s]));
  S.server = server;
}

export function clearDraft() {
  S.draft = emptyDraft();
  S.invalid = {};
  S.errors = {};
  S.warnings = [];
  S.summary = '';
}

export const valueState = (key) => (S.server && S.server.values[key]) || { value: null, stored: null, source: 'default' };
export const isEnv = (key) => valueState(key).source === 'env';
export const inDraft = (key) => key in S.draft.values;

// The value the row shows: the draft's, else the server's effective value.
export function valueOf(key) {
  if (inDraft(key)) return S.draft.values[key] === null ? S.spec[key].default : S.draft.values[key];
  return valueState(key).value;
}

// Puts `v` (stored units; null: reset to the default) into the draft, or takes the key out when it is no change.
export function setValue(key, v) {
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const stored = valueState(key).stored ?? null;
  if (v === null || same(v, spec.default)) {
    if (stored === null) delete S.draft.values[key]; else S.draft.values[key] = null;
  } else if (same(v, stored)) {
    delete S.draft.values[key];
  } else {
    S.draft.values[key] = v;
  }
  delete S.invalid[key];
  changed();
}

export const listState = (name) => (S.server && S.server.lists[name]) || { set: false, items: [], errors: {} };

// The items of a list as the page shows them: the draft's, else the server's (the defaults while it is not set).
export function listItems(name) {
  if (name in S.draft.lists) return clone(S.draft.lists[name] === null ? S.schema.lists[name].default_items : S.draft.lists[name]);
  return clone(listState(name).items);
}

export function setList(name, items) {
  const srv = listState(name);
  const defaults = S.schema.lists[name].default_items;
  if (items === null || sameItems(items, defaults)) {
    if (srv.set) S.draft.lists[name] = null; else delete S.draft.lists[name];
  } else if (sameItems(items, srv.items) && srv.set) {
    delete S.draft.lists[name];
  } else {
    S.draft.lists[name] = clone(items);
  }
  changed();
}

export function setSecret(slot, text) {
  if (text === undefined) delete S.draft.secrets[slot]; else S.draft.secrets[slot] = text;
  changed();
}

export const changeCount = () => Object.keys(S.draft.values).length + Object.keys(S.draft.lists).length + Object.keys(S.draft.secrets).length;
export const hasProblems = () => Object.keys(S.invalid).length > 0 || Object.keys(S.errors).length > 0;

// A document {values, lists} (a version, an imported file) as a draft against the loaded state: only what differs from
// it. A key the document does not have goes back to its default. Keys pinned by the environment are left alone, and so
// are the ones in `doc.kept` (the server refused the file's value: the current one stays).
export function applyDocument(doc) {
  clearDraft();
  const values = doc.values || {};
  const kept = new Set(doc.kept || []);
  for (const key of Object.keys(S.spec)) {
    if (isEnv(key) || kept.has(key)) continue;
    setValueQuiet(key, key in values, values[key]);
  }
  for (const name of Object.keys(S.schema.lists)) {
    const items = doc.lists ? doc.lists[name] : undefined;
    const srv = listState(name);
    if (items === undefined || items === null) { if (srv.set) S.draft.lists[name] = null; } else if (!srv.set || !sameItems(items, srv.items)) S.draft.lists[name] = clone(items);
  }
  changed();
}

// One key of a document. A value the document sets that is what the page already has (stored, or the default while
// nothing is stored) is no change: an Export imported again must leave the draft empty.
function setValueQuiet(key, present, v) {
  const spec = S.spec[key];
  const stored = valueState(key).stored ?? null;
  if (!present || v === null) {
    if (stored !== null) S.draft.values[key] = null;
  } else if (same(v, stored)) {
    // already stored so
  } else if (same(v, spec.default)) {
    if (stored !== null) S.draft.values[key] = null;
  } else {
    S.draft.values[key] = v;
  }
}

// After a 409: drop the parts of the draft that changed elsewhere (`conflicts`: keys and list names); returns them.
export function dropConflicts(conflicts) {
  const dropped = [];
  for (const name of conflicts) {
    if (name in S.draft.values) { delete S.draft.values[name]; dropped.push(name); }
    if (name in S.draft.lists) { delete S.draft.lists[name]; dropped.push(name); }
    delete S.invalid[name];
  }
  return dropped;
}
