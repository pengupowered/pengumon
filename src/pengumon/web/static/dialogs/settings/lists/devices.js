// The devices list editor of the Settings page (the "Devices" list of the "Devices and services" section).
//
// The list contract (design 7, `dialogs/settings/lists/<name>.js`): the default export has
//   render(container, items, ctx)  draws the cards from the saved items
//   collect()                      the items as they are in the draft now (what the PUT sends under lists.devices)
//   problems(errors)               shows server messages ([{where, message}]) under their fields; returns the ones it
//                                  could not place
// and ctx is { schema, markDirty(), canEdit(), openDeviceSetup(id), itemErrors }. `schema` is the /api/settings/schema
// body (lists.devices.item_fields and lists.devices.drivers[driver].options generate the form); `itemErrors` is
// `lists.devices.errors` of GET /api/settings: item id -> why that device is not running.
//
// Passwords never come here: a device's sign-in is entered in the device-setup dialog (ctx.openDeviceSetup) and kept
// in secrets.json. The id of a saved device is read-only (its history and sign-in are kept under it); to change it,
// remove the device and add it again. Nothing is applied until the page saves.

import { state } from '../../../js/state.js';
import { escapeHtml, icon } from '../../../js/util.js';

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const e = escapeHtml;

let root = null;
let ctx = null;
let drivers = {};
let items = [];          // the draft, in order: working copies with an extra `_key` (stable across edits) and `_saved`
let removed = [];        // saved devices taken out of the draft: shown with Undo, not collected
let open = new Set();    // _keys of the expanded cards
let serverErrors = {};   // _key -> { field: message } from problems()
let seq = 0;

const spec = () => (ctx && ctx.schema && ctx.schema.lists && ctx.schema.lists.devices) || { drivers: {}, item_fields: [] };
const driverOf = (item) => drivers[item.driver] || { label: item.driver, options: [], url_schemes: ['http', 'https'], default_poll_seconds: null, min_poll_seconds: 0, default_username: 'admin' };
const clone = (v) => JSON.parse(JSON.stringify(v));

function fromSaved(raw) {
  return { ...clone(raw), tls: { verify: true, ca_file: null, ...(raw.tls || {}) }, options: { ...(raw.options || {}) }, _key: `d${++seq}`, _saved: true };
}

function hostOf(url) {
  try { return new URL(url).hostname || url; } catch { return url || ''; }
}

function suggestId(driver) {
  const base = (driver || 'device').replace(/[^A-Za-z0-9_-]/g, '_');
  const taken = new Set([...items, ...removed].map((i) => i.id));
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}${n}`)) n += 1;
  return `${base}${n}`;
}

// ---- local checks (the server's validation is the real one; these show the obvious at once) ----

function localProblems(item) {
  const out = {};
  const id = (item.id || '').trim();
  if (!id) out.id = 'Give the device an id.';
  else if (!ID_RE.test(id)) out.id = 'Use 1-64 letters, digits, hyphens or underscores.';
  else if (items.filter((i) => i.id.trim().toLowerCase().replace(/-/g, '_') === id.toLowerCase().replace(/-/g, '_')).length > 1) out.id = 'Another device has this id (ids that differ only in case or - versus _ share their sign-in variables).';
  const schemes = driverOf(item).url_schemes || ['http', 'https'];
  const url = (item.url || '').trim();
  if (!url) out.url = 'Enter the address of the device.';
  else if (!schemes.some((s) => url.startsWith(`${s}://`))) out.url = `The address must start with ${schemes.map((s) => `${s}://`).join(' or ')}.`;
  else if (/\/\/[^/]*@/.test(url)) out.url = 'The address must not contain a user name or password.';
  const poll = item.poll_interval_seconds;
  if (poll !== null && poll !== undefined && !(Number(poll) > 0)) out.poll_interval_seconds = 'Must be more than 0 seconds.';
  if (item.tls && item.tls.ca_file && item.tls.verify === false) out['tls.ca_file'] = 'A CA file is only used to verify the certificate; turn verification on or clear this.';
  return out;
}

const problemOf = (item, field) => localProblems(item)[field] || (serverErrors[item._key] || {})[field] || '';

// ---- markup ----

function field(item, name, label, control, hint = '') {
  const message = problemOf(item, name);
  return `<div class="field dv-f${message ? ' invalid' : ''}"><label>${e(label)}${control}</label>`
    + (hint ? `<span class="hint">${hint}</span>` : '')
    + `<span class="dv-err" data-err="${e(name)}" role="${message ? 'alert' : 'none'}">${e(message)}</span></div>`;
}

function optionControl(item, opt) {
  const current = item.options[opt.name];
  const common = `data-opt="${e(opt.name)}"`;
  if (opt.type === 'boolean') {
    const on = current === undefined ? opt.default !== false : current;
    return `<input type="checkbox" ${common} ${on ? 'checked' : ''}>`;
  }
  if (opt.type === 'map') {
    const rows = Object.entries(current || {});
    return `<div class="dv-map" ${common}>${rows.map(([k, v], n) => `<div class="dv-map-row"><input class="input" data-map-k="${n}" value="${e(k)}" placeholder="Key" aria-label="Key"><input class="input" data-map-v="${n}" value="${e(v)}" placeholder="Value" aria-label="Value">`
      + `<button type="button" class="btn sm icon" data-act="map-del" data-opt="${e(opt.name)}" data-n="${n}" aria-label="Remove this entry" data-needs-edit>${icon('close')}</button></div>`).join('')}`
      + `<button type="button" class="btn sm" data-act="map-add" data-opt="${e(opt.name)}" data-needs-edit>${icon('plus')}Add an entry</button></div>`;
  }
  if (opt.type === 'choice' && opt.choices) {
    return `<select class="input" ${common}>${opt.choices.map((c) => `<option value="${e(c)}"${String(current === undefined ? opt.default : current) === String(c) ? ' selected' : ''}>${e(c)}</option>`).join('')}</select>`;
  }
  if (opt.type === 'number' || opt.type === 'integer') {
    const min = opt.min !== null && opt.min !== undefined ? ` min="${opt.min}"` : '';
    const max = opt.max !== null && opt.max !== undefined ? ` max="${opt.max}"` : '';
    return `<input class="input" type="number" inputmode="decimal" step="${opt.type === 'integer' ? 1 : 'any'}"${min}${max} ${common} value="${current === undefined || current === null ? '' : e(current)}" placeholder="${opt.default === null || opt.default === undefined ? '' : `Default ${e(opt.default)}`}">`;
  }
  return `<input class="input" type="text" ${common} value="${current === undefined || current === null ? '' : e(current)}" placeholder="${opt.default === null || opt.default === undefined ? '' : `Default ${e(opt.default)}`}">`;
}

const OPTION_LABEL = (name) => name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

function optionsMarkup(item) {
  const options = driverOf(item).options || [];
  if (!options.length) return '';
  return `<fieldset class="dv-opts"><legend>${e(driverOf(item).label)} options</legend>${options.map((opt) => {
    const wrap = `<div class="field dv-f${opt.type === 'boolean' ? ' dv-check' : ''}"><label>${opt.type === 'boolean' ? `${optionControl(item, opt)}<span>${e(OPTION_LABEL(opt.name))}</span>` : `${e(OPTION_LABEL(opt.name))}${opt.type === 'map' ? '' : optionControl(item, opt)}`}</label>`
      + `${opt.type === 'map' ? optionControl(item, opt) : ''}`
      + `<span class="dv-err" data-err="options.${e(opt.name)}">${e((serverErrors[item._key] || {})[`options.${opt.name}`] || '')}</span></div>`;
    return wrap;
  }).join('')}</fieldset>`;
}

function formMarkup(item) {
  const d = driverOf(item);
  const floor = d.min_poll_seconds > 0 ? ` and at least ${e(d.min_poll_seconds)} s (a lower value is raised to it)` : '';
  const driverSelect = `<select class="input" data-f="driver">${Object.keys(drivers).sort().map((k) => `<option value="${e(k)}"${k === item.driver ? ' selected' : ''}>${e(drivers[k].label || k)}</option>`).join('')}</select>`;
  const placeholder = (d.url_schemes || ['http']).includes('snmp') ? 'snmp://192.0.2.1' : 'http://192.0.2.1';
  return `<div class="dv-form">`
    + field(item, 'id', 'Id', `<input class="input mono" data-f="id" value="${e(item.id)}" maxlength="64" autocomplete="off" spellcheck="false"${item._saved ? ' readonly' : ''}>`,
      item._saved ? 'Fixed once saved: its history and sign-in are kept under this id.' : 'Letters, digits, - and _. Fixed once saved.')
    + field(item, 'driver', 'Kind of device', driverSelect)
    + field(item, 'url', 'Address', `<input class="input mono" data-f="url" value="${e(item.url)}" maxlength="255" placeholder="${placeholder}" autocomplete="off" spellcheck="false">`,
      `Must start with ${(d.url_schemes || ['http', 'https']).map((s) => `${e(s)}://`).join(' or ')}. No user name or password: the sign-in is entered with Set up.`)
    + field(item, 'model', 'Display name', `<input class="input" data-f="model" value="${e(item.model || '')}" maxlength="100" placeholder="Optional">`, 'Shown on the device card when the device does not report a model.')
    + field(item, 'username', 'User name', `<input class="input" data-f="username" value="${e(item.username || '')}" maxlength="64" placeholder="${e(d.default_username || 'admin')}" autocomplete="off">`)
    + field(item, 'poll_interval_seconds', 'Poll every (seconds)', `<input class="input" type="number" inputmode="decimal" min="0" step="any" data-f="poll_interval_seconds" value="${item.poll_interval_seconds === null || item.poll_interval_seconds === undefined ? '' : e(item.poll_interval_seconds)}" placeholder="Default ${e(d.default_poll_seconds)}">`,
      `Default ${e(d.default_poll_seconds)} s${floor}.`)
    + `<div class="field dv-f dv-check"><label><input type="checkbox" data-f="enabled"${item.enabled === false ? '' : ' checked'}><span>Poll this device</span></label><span class="hint">Switched off, it stays listed and says so.</span></div>`
    + `<fieldset class="dv-opts"><legend>Certificate (https addresses)</legend>`
    + `<div class="field dv-f dv-check"><label><input type="checkbox" data-f="tls.verify"${item.tls.verify === false ? '' : ' checked'}><span>Check the device's certificate</span></label><span class="hint">Turn off for a certificate the device made itself.</span></div>`
    + field(item, 'tls.ca_file', 'CA file', `<input class="input mono" data-f="tls.ca_file" value="${e(item.tls.ca_file || '')}" placeholder="Optional, a path on this host">`, 'A PEM file to check the certificate against, for a home CA or the device\'s own certificate.')
    + `</fieldset>`
    + optionsMarkup(item)
    + `</div>`;
}

function summaryMarkup(item) {
  const d = driverOf(item);
  const live = (state.devices || []).find((x) => x.id === item.id);
  const chips = [`<span class="chip">${e(d.label || item.driver)}</span>`];
  const host = hostOf(item.url);
  if (host) chips.push(`<span class="chip mono">${e(host)}</span>`);
  if (item.enabled === false) chips.push('<span class="chip">Disabled</span>');
  if (!item._saved) chips.push('<span class="chip st st-warn">New</span>');
  else if (live && live.has_credentials === false && item.enabled !== false) chips.push('<span class="chip st st-warn">Not set up</span>');
  return chips.join('');
}

function cardMarkup(item, index) {
  const expanded = open.has(item._key);
  const failure = (ctx.itemErrors || {})[item.id];
  const local = Object.values(localProblems(item)).length || Object.keys(serverErrors[item._key] || {}).length;
  const title = item.model || item.id || 'New device';
  return `<li class="dv-card${expanded ? ' open' : ''}${local ? ' bad' : ''}" data-key="${e(item._key)}">`
    + `<div class="dv-head"><button type="button" class="dv-toggle" data-act="toggle" aria-expanded="${expanded}" aria-controls="dv-body-${e(item._key)}">${icon('chevron')}<span class="dv-title">${e(title)}</span>${item.model && item.id ? `<span class="dv-id mono">${e(item.id)}</span>` : ''}</button>`
    + `<span class="dv-chips">${summaryMarkup(item)}</span>`
    + `<span class="dv-tools"><button type="button" class="btn sm icon" data-act="up" aria-label="Move up"${index === 0 ? ' disabled' : ''} data-needs-edit>${icon('up')}</button>`
    + `<button type="button" class="btn sm icon dv-down" data-act="down" aria-label="Move down"${index === items.length - 1 ? ' disabled' : ''} data-needs-edit>${icon('up')}</button></span></div>`
    + (failure ? `<div class="dv-fail" role="alert">${icon('warn')}<span>${e(failure)}</span></div>` : '')
    + (local ? `<div class="dv-fail" role="alert">${icon('warn')}<span>${e(Object.values(localProblems(item))[0] || Object.values(serverErrors[item._key])[0])}</span></div>` : '')
    + `<div class="dv-body" id="dv-body-${e(item._key)}"${expanded ? '' : ' hidden'}>${expanded ? formMarkup(item) : ''}`
    + `<div class="dv-actions">`
    + (item._saved ? `<button type="button" class="btn sm" data-act="setup" data-needs-edit>${icon('lock')}Set up the sign-in</button>`
      : '<span class="hint">Save first, then use Set up to enter the device\'s password.</span>')
    + `<button type="button" class="btn sm danger" data-act="remove" data-needs-edit>Remove</button></div></div></li>`;
}

// What Save does to the path hops that read this device (the path editor's "device" link): they are unlinked, not deleted.
function hopsText(item) {
  if (!item._hops) return 'Path hops linked to it will be unlinked.';
  if (!item._hops.length) return 'No path hop is linked to it.';
  return `Path hops linked to it will be unlinked, and stop showing its readings: ${item._hops.map(e).join(', ')}.`;
}

async function findHops(item) {
  try {
    const res = await fetch('/api/path', { cache: 'no-store' });
    if (!res.ok) return;
    item._hops = ((await res.json()).hops || []).filter((h) => h.device_id === item.id).map((h) => h.name);
    if (removed.includes(item)) draw();
  } catch (err) { /* the general sentence stays */ }
}

function draw() {
  if (!root) return;
  const removedMarkup = removed.length
    ? `<ul class="dv-removed">${removed.map((r, n) => `<li><span><b class="mono">${e(r.id)}</b> will be removed when you save. Its stored readings stay until retention removes them. ${hopsText(r)}</span> <button type="button" class="btn sm" data-act="undo" data-n="${n}" data-needs-edit>Undo</button></li>`).join('')}</ul>` : '';
  root.innerHTML = `<div class="dv"><ul class="dv-list">${items.map(cardMarkup).join('')}</ul>`
    + (items.length ? '' : '<p class="empty compact">No devices yet. Pengumon then monitors only the probes of the path.</p>')
    + removedMarkup
    + `<div class="dv-add"><button type="button" class="btn" data-act="add" data-needs-edit>${icon('plus')}Add a device</button></div></div>`;
  if (!ctx.canEdit()) {
    root.querySelectorAll('input, select, button[data-needs-edit]').forEach((el) => { if (el.dataset.act !== 'toggle') el.disabled = true; });
  }
}

// ---- events ----

function itemOf(el) {
  const card = el.closest('.dv-card');
  return card ? items.find((i) => i._key === card.dataset.key) : null;
}

function refreshCard(item) {
  const card = root.querySelector(`.dv-card[data-key="${item._key}"]`);
  if (!card) return;
  card.querySelector('.dv-chips').innerHTML = summaryMarkup(item);
  card.querySelector('.dv-title').textContent = item.model || item.id || 'New device';
  card.classList.toggle('bad', !!Object.keys(localProblems(item)).length);
  card.querySelectorAll('[data-err]').forEach((el) => {
    const message = problemOf(item, el.dataset.err);
    el.textContent = message;
    el.closest('.dv-f').classList.toggle('invalid', !!message);
  });
}

function setPath(item, path, value) {
  if (path.startsWith('tls.')) item.tls[path.slice(4)] = value;
  else item[path] = value;
}

function onInput(ev) {
  const el = ev.target;
  const item = itemOf(el);
  if (!item || !ctx.canEdit()) return;
  if (el.dataset.f) {
    const f = el.dataset.f;
    let value = el.type === 'checkbox' ? el.checked : el.value;
    if (f === 'poll_interval_seconds') value = el.value === '' ? null : Number(el.value);
    else if (f === 'tls.ca_file' || f === 'model') value = el.value === '' ? null : el.value;
    if (f === 'driver') { item.driver = value; item.options = {}; open.add(item._key); draw(); ctx.markDirty(); return; }
    setPath(item, f, value);
    if (serverErrors[item._key]) delete serverErrors[item._key][f];
  } else if (el.dataset.opt && !el.closest('.dv-map')) {
    const name = el.dataset.opt;
    if (el.type === 'checkbox') item.options[name] = el.checked;
    else if (el.value === '') delete item.options[name];
    else item.options[name] = el.type === 'number' ? Number(el.value) : el.value;
  } else if (el.dataset.mapK !== undefined || el.dataset.mapV !== undefined) {
    const map = el.closest('.dv-map');
    const rows = [...map.querySelectorAll('.dv-map-row')].map((r) => [r.querySelector('[data-map-k]').value.trim(), r.querySelector('[data-map-v]').value]);
    item.options[map.dataset.opt] = Object.fromEntries(rows.filter(([k]) => k));
  } else return;
  refreshCard(item);
  ctx.markDirty();
}

function move(item, by) {
  const at = items.indexOf(item);
  const to = at + by;
  if (to < 0 || to >= items.length) return;
  items.splice(at, 1);
  items.splice(to, 0, item);
  draw();
  ctx.markDirty();
}

function onClick(ev) {
  const el = ev.target.closest('[data-act]');
  if (!el || el.disabled || !root.contains(el)) return;
  const act = el.dataset.act;
  const item = itemOf(el);
  if (act === 'toggle' && item) {
    if (open.has(item._key)) open.delete(item._key); else open.add(item._key);
    draw();
    return;
  }
  if (!ctx.canEdit()) return;
  if (act === 'add') {
    const driver = Object.keys(drivers).sort()[0] || '';
    const fresh = { id: suggestId(driver), driver, url: '', enabled: true, poll_interval_seconds: null, username: '', tls: { verify: true, ca_file: null }, model: null, options: {}, _key: `d${++seq}`, _saved: false };
    items.push(fresh);
    open.add(fresh._key);
    draw();
    root.querySelector(`.dv-card[data-key="${fresh._key}"] [data-f=url]`)?.focus();
    ctx.markDirty();
  } else if (act === 'remove' && item) {
    items.splice(items.indexOf(item), 1);
    open.delete(item._key);
    if (item._saved) { removed.push(item); findHops(item); }
    draw();
    ctx.markDirty();
  } else if (act === 'undo') {
    const back = removed.splice(Number(el.dataset.n), 1)[0];
    if (back) { items.push(back); draw(); ctx.markDirty(); }
  } else if (act === 'up' && item) move(item, -1);
  else if (act === 'down' && item) move(item, 1);
  else if (act === 'setup' && item) ctx.openDeviceSetup(item.id);
  else if ((act === 'map-add' || act === 'map-del') && item) {
    const map = { ...(item.options[el.dataset.opt] || {}) };
    if (act === 'map-add') map[''] = '';
    else delete map[Object.keys(map)[Number(el.dataset.n)]];
    item.options[el.dataset.opt] = map;
    draw();
    ctx.markDirty();
  }
}

// ---- the module ----

function cleaned(item) {
  const out = { id: item.id.trim(), driver: item.driver, url: item.url.trim(), enabled: item.enabled !== false };
  if (item.poll_interval_seconds !== null && item.poll_interval_seconds !== undefined) out.poll_interval_seconds = item.poll_interval_seconds;
  if (item.username && item.username.trim()) out.username = item.username.trim();
  if (item.model && item.model.trim()) out.model = item.model.trim();
  const tls = { verify: item.tls.verify !== false };
  if (item.tls.ca_file && item.tls.ca_file.trim()) tls.ca_file = item.tls.ca_file.trim();
  if (!tls.verify || tls.ca_file) out.tls = tls;
  const options = {};
  for (const [name, v] of Object.entries(item.options || {})) {
    if (v === '' || v === null || v === undefined) continue;
    if (typeof v === 'object' && !Array.isArray(v)) {  // a map: rows without a key are not entries
      const rows = Object.fromEntries(Object.entries(v).filter(([k]) => k !== ''));
      if (Object.keys(rows).length) options[name] = rows;
    } else options[name] = v;
  }
  if (Object.keys(options).length) out.options = options;
  return out;
}

export default {
  id: 'devices',

  render(container, saved, context) {
    root = container;
    ctx = context;
    drivers = spec().drivers || {};
    items = (saved || []).map(fromSaved);
    removed = [];
    open = new Set();
    serverErrors = {};
    root.classList.add('dv-root');
    if (!root.dataset.dvBound) {  // one listener pair per container, however often the list is drawn again
      root.addEventListener('input', onInput);
      root.addEventListener('change', onInput);
      root.addEventListener('click', onClick);
      root.dataset.dvBound = '1';
    }
    draw();
  },

  collect() {
    return items.map(cleaned);
  },

  // The editor's own checks, for the page to block Save on: one message per problem.
  localProblems() {
    return items.flatMap((i) => Object.entries(localProblems(i)).map(([where, message]) => ({ where: `lists.devices[${items.indexOf(i)}].${where}`, message })));
  },

  problems(errors) {
    serverErrors = {};
    const unplaced = [];
    for (const err of errors || []) {
      const byIndex = /^lists\.devices\[(\d+)\](?:\.(.+))?$/.exec(err.where);
      const byId = /^devices\.([^.]+)(?:\.(.+))?$/.exec(err.where);
      let item = null;
      let fieldName = '';
      if (byIndex) { item = items[Number(byIndex[1])]; fieldName = byIndex[2] || ''; }
      else if (byId) { item = items.find((i) => i.id === byId[1]); fieldName = byId[2] || ''; }
      if (!item) { unplaced.push(err); continue; }
      (serverErrors[item._key] = serverErrors[item._key] || {})[fieldName || 'id'] = err.message;
      open.add(item._key);
    }
    draw();
    return unplaced;
  }
};
