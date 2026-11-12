// Naming things in words for the notes and the summary at the foot of the page: a setting as the page labels it (with its
// group or section when the label alone is not clear), a list item by its list and its name, and what stops Save.

import { sameItems } from './model.js';
import { S, listItems, listState, valueState } from './store.js';
import { shownValue } from './fields.js';

const sectionTitle = (id) => ((S.schema.sections.find((s) => s.id === id) || {}).title || '');

// "Busy line from", "Quiet hours: From", and with the section ("Alerts: Quiet hours: From") when another setting would
// read the same.
function settingName(key) {
  const spec = S.spec[key];
  if (!spec) return key;
  const inGroup = (s) => (s.group && !s.label.toLowerCase().includes(s.group.split(' ')[0].toLowerCase()) ? `${s.group}: ${s.label}` : s.label);
  const name = inGroup(spec);
  const alike = S.schema.settings.some((o) => o.key !== key && inGroup(o) === name);
  const section = sectionTitle(spec.section);
  return alike && section ? `${section}: ${name}` : name;
}

const listLabel = (name) => ((S.schema.lists[name] || {}).label || name);

// The name of item `i` of list `name` in the draft, else the id, else its place.
function itemName(name, i) {
  const item = listItems(name)[i] || {};
  return String(item.name || item.id || `item ${Number(i) + 1}`);
}

// The items of the draft list `name` that differ from the server's current list, by name.
function changedItems(name, draftItems) {
  const srv = listState(name).items || [];
  return (draftItems || []).filter((it) => {
    const same = srv.find((o) => o.id === it.id);
    return !same || !sameItems([same], [it]);
  }).map((it) => String(it.name || it.id || '?'));
}

// For the 409 note: one phrase per setting or list that changed elsewhere, taken before the draft drops it. A setting is
// named in full with the value that was kept ("Busy line from (now 55 %)"); a list by its name and the items of the
// draft that were not kept ("Webhooks (your changes to Slack, Pager were not kept)").
export function conflictNames(conflicts) {
  return conflicts.map((c) => {
    if (S.schema.lists[c]) {
      const mine = changedItems(c, S.draft.lists[c]);
      return `${listLabel(c)}${mine.length ? ` (not kept: ${mine.join(', ')})` : ''}`;
    }
    const spec = S.spec[c];
    if (!spec) return c;
    return `${settingName(c)} (now ${shownValue(spec, valueState(c).value)})`;
  });
}

// What stops Save, in page order: [{name, message, key | list + i}]. `local` is the editors' own checks
// ([{where, message}]).
export function problemList(local = []) {
  const out = [];
  const seen = new Set();
  const add = (p) => { const id = `${p.name}|${p.message}`; if (!seen.has(id)) { seen.add(id); out.push(p); } };
  const fromWhere = (where, message) => {
    let m = /^values\.(.+)$/.exec(where);
    if (m) return add({ name: settingName(m[1]), message, key: m[1] });
    m = /^lists\.([a-z_]+)\[(\d+)\]/.exec(where);
    if (m) return add({ name: `${listLabel(m[1])}: ${itemName(m[1], m[2])}`, message, list: m[1], i: Number(m[2]) });
    m = /^devices\.([^.]+)/.exec(where);
    if (m) return add({ name: `Devices: ${m[1]}`, message, list: 'devices', id: m[1] });
    m = /^secrets\.(.+)$/.exec(where);
    if (m) return add({ name: ((S.schema.secrets.find((s) => s.slot === m[1]) || {}).label || m[1]), message });
    return add({ name: where, message });
  };
  Object.entries(S.invalid).forEach(([key, message]) => {
    const w = /^webhook-url:(.+)$/.exec(key);
    if (w) {
      const i = listItems('webhooks').findIndex((it) => it.id === w[1]);
      add({ name: `Webhooks: ${i >= 0 ? itemName('webhooks', i) : w[1]}`, message, list: 'webhooks', i });
    } else if (S.spec[key]) add({ name: settingName(key), message, key });
    else add({ name: key, message });
  });
  Object.entries(S.errors).forEach(([where, message]) => fromWhere(where, message));
  local.forEach((p) => fromWhere(p.where, p.message));
  return out;
}

// "2 problems: Busy line from: Enter a value from 1 to 100 %; Webhooks: Slack: URL needed."
export function problemSummary(list) {
  if (!list.length) return '';
  const shown = list.slice(0, 3).map((p) => `${p.name}: ${String(p.message).replace(/[.\s]+$/, '')}`);
  const more = list.length > 3 ? `; and ${list.length - 3} more` : '';
  return `${list.length === 1 ? '1 problem' : `${list.length} problems`}: ${shown.join('; ')}${more}.`;
}

// A FastAPI `detail` as one sentence: a string as it is, a 422 list as its messages, else the fallback.
export function detailText(detail, fallback) {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const text = detail.map((d) => String((d && d.msg) || '').replace(/^Value error, /, '')).filter(Boolean).join(' · ');
    if (text) return text;
  }
  return fallback;
}
