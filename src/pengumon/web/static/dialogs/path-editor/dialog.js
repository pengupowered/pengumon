// The path editor: the probe path as a draft of hop cards in path order (GET /api/path), edited here and applied in
// one PUT /api/path; nothing goes live until Apply. Opened by Path health's Edit button, by its first-run banner (with
// a discovery's draft, data-arg "discovery:<id>") and by Rediscover inside it. A removed hop stops being probed; its
// history is kept (hidden) unless "delete its history" is ticked. A draft from discovery shows each hop's confidence,
// its reasons (evidence), a suggested device driver and the difference from the live path (new / changed / not found).

import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { closeModal, deviceName, escapeHtml, hostProblem, icon, openModal } from '../../js/util.js';
import { ROLES, ROLE_HINT, ROLE_WORD, errorText, fetchPath, startDiscovery, waitDiscovery } from '../../js/path-api.js';
import { NO_IPV6, NO_IPV6_WHY } from '../../js/path.js';

const $ = (id) => document.getElementById(id);
const UI_FIELDS = ['joined', 'added', 'evidence', 'confidence', 'suggested_driver', 'status'];
const CONFIDENCE = { high: ['good', 'Sure'], likely: ['unknown', 'Likely'], unknown: ['warn', 'Unsure'] };
const STATUS_CHIP = { new: ['good', 'New'], changed: ['warn', 'Changed'], gone: ['crit', 'Not found by discovery'] };

let draft = null; // {base, hops, source: 'live' | 'discovery' | 'import', notes}
let removed = []; // [{hop, index, live, deleteHistory}]
let liveIds = new Set();
let tests = new Map(); // hop id -> its last test result, as markup
let applying = false;

function note(level, html) {
  $('pathEditorNote').innerHTML = html ? `<div class="result st-${level}">${icon(level === 'good' ? 'ok' : level === 'crit' ? 'down' : level === 'warn' ? 'warn' : 'unknown')}<span>${html}</span></div>` : '';
}

// ---------- the draft ----------
function load(path, hops, source, notes = []) {
  draft = { base: path.revision, source, notes, hops: hops.slice().sort((a, b) => a.position - b.position).map((h) => ({ ...h })) };
  draft.hops.forEach((h, i) => { h.joined = i > 0 && !!h.group && h.group === draft.hops[i - 1].group; });
  liveIds = new Set(path.hops.map((h) => h.id));
  removed = [];
  tests = new Map();
  if (source === 'import') { // the live hops the file leaves out: removed, with an undo each
    const kept = new Set(draft.hops.map((h) => h.id));
    path.hops.filter((h) => !kept.has(h.id)).forEach((h) => removed.push({ hop: { ...h, joined: false }, index: h.position, live: true, deleteHistory: false }));
  }
  render();
}

// Groups from the "one node with the hop before" ticks: a run of joined hops of one role shares a group (its first
// existing group name, else "internet" for public servers ("internet_v6" for IPv6 ones, Path health's own node) or one
// made from the first hop's id); a lone hop has none.
function grouped(hops) {
  const runs = [];
  hops.forEach((h, i) => {
    const joins = i > 0 && h.joined && hops[i - 1].category === h.category;
    if (joins) runs[runs.length - 1].push(h); else runs.push([h]);
  });
  const used = new Set();
  const out = [];
  runs.forEach((run) => {
    let group = null;
    if (run.length > 1) {
      const v6 = run.every((h) => familyOf(h) === 6);
      const base = (run.find((h) => h.group) || {}).group
        || (run[0].category === 'public' ? (v6 ? 'internet_v6' : 'internet') : `${run[0].category}_${run[0].id}`.slice(0, 64));
      group = base;
      for (let k = 2; used.has(group); k++) group = `${base.slice(0, 60)}_${k}`;
      used.add(group);
    }
    run.forEach((h) => out.push({ ...h, group }));
  });
  return out;
}

function payload() {
  return grouped(draft.hops).map((h, i) => {
    const hop = { ...h, position: i };
    UI_FIELDS.forEach((f) => delete hop[f]);
    return hop;
  });
}

function slug(text) {
  return String(text).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

function freshId(category, address) {
  const taken = new Set([...draft.hops, ...removed.map((r) => r.hop)].map((h) => h.id));
  const base = `${category}_${slug(address) || 'hop'}`.slice(0, 56);
  let id = base;
  for (let k = 2; taken.has(id); k++) id = `${base}_${k}`;
  return id;
}

// ---------- rendering ----------
const options = (pairs, value) => pairs.map(([v, w]) => `<option value="${escapeHtml(v)}"${v === value ? ' selected' : ''}>${escapeHtml(w)}</option>`).join('');

// Discovery's guess of the device a hop is, by the driver that reads it (the page names no vendor): Connect opens the
// device setup of a configured device with that driver, else how to configure one.
function suggestion(h) {
  const driver = h.suggested_driver;
  if (!driver) return '';
  const device = state.devices.find((d) => d.driver === driver);
  const how = device
    ? `<button type="button" class="btn sm outline" data-action="connectPathDevice" data-arg="${escapeHtml(h.id)}" data-device="${escapeHtml(device.id)}">${icon('lock')}Connect ${escapeHtml(deviceName(device))}</button>`
    : `<span>Add it under <code>devices:</code> in config.yaml (driver <code>${escapeHtml(driver)}</code>) to read it.</span>`;
  return `<div class="pe-suggest">${icon('info', 'sm')}<span>Looks like a device the <code>${escapeHtml(driver)}</code> driver can read: connect it for more detail.</span>${how}</div>`;
}

function card(h, i, hops) {
  const n = i + 1;
  const id = escapeHtml(h.id);
  const prev = hops[i - 1];
  const canJoin = !!prev && prev.category === h.category;
  const chips = [];
  if (h.confidence && CONFIDENCE[h.confidence]) {
    const [level, word] = CONFIDENCE[h.confidence];
    chips.push(`<span class="chip st st-${level}" title="How sure discovery is of this hop's role">${escapeHtml(word)}</span>`);
  }
  if (STATUS_CHIP[h.status]) chips.push(`<span class="chip st st-${STATUS_CHIP[h.status][0]}">${STATUS_CHIP[h.status][1]}</span>`);
  if (h.added) chips.push('<span class="chip st st-good">Added</span>');
  if (h.enabled === false) chips.push('<span class="chip">Paused</span>');
  const devices = [['', 'None'], ...state.devices.map((d) => [d.id, deviceName(d)])];
  if (h.device_id && !state.devices.some((d) => d.id === h.device_id)) devices.push([h.device_id, h.device_id]);
  const ping = h.ignores_ping === true ? 'yes' : h.ignores_ping === false ? 'no' : 'auto';
  const evidence = (h.evidence || []).length
    ? `<ul class="pe-ev" aria-label="Why discovery thinks so">${h.evidence.map((e) => `<li>${escapeHtml(e.text)}</li>`).join('')}</ul>` : '';
  return `<li class="pe-hop${h.status === 'gone' ? ' gone' : ''}" data-hop="${id}">
    <div class="pe-hop-h"><span class="pe-n num" aria-hidden="true">${n}</span>
      <input class="input pe-name" type="text" value="${escapeHtml(h.name)}" maxlength="100" data-input="editPathHop" data-change="editPathHop" data-field="name" data-arg="${id}" aria-label="Name of hop ${n}">
      ${chips.join('')}</div>
    <div class="pe-hop-sub"><span class="mono pe-addr" title="${escapeHtml(h.address)}">${escapeHtml(h.address)}</span>${familyOf(h) === 6 ? '<span class="chip">IPv6</span>' : ''}<span class="pe-hint">${escapeHtml(ROLE_WORD[h.category] || h.category)}: ${escapeHtml(ROLE_HINT[h.category] || '')}</span></div>
    <div class="pe-hop-b">
      <label class="field"><span>Role</span><select class="input" data-change="editPathHop" data-field="category" data-arg="${id}" aria-label="Role of hop ${n}">${options(ROLES, h.category)}</select></label>
      <label class="field"><span>Ignores ping</span><select class="input" data-change="editPathHop" data-field="ignores_ping" data-arg="${id}" aria-label="Does hop ${n} ignore ping">${options([['auto', 'Decide automatically'], ['yes', 'Yes'], ['no', 'No']], ping)}</select></label>
      ${isName(h.address) ? `<label class="field"><span>Probe the name's</span><select class="input" data-change="editPathHop" data-field="family" data-arg="${id}" aria-label="Address family of hop ${n}">${options([['4', 'IPv4 address'], ['6', 'IPv6 address']], String(h.family || 4))}</select></label>` : ''}
      <label class="field"><span>Device</span><select class="input" data-change="editPathHop" data-field="device_id" data-arg="${id}" aria-label="Device of hop ${n}">${options(devices, h.device_id || '')}</select></label>
      ${i ? `<label class="check pe-join" title="${canJoin ? 'Path health shows the two as one node' : 'Only hops of the same role can be one node'}"><input type="checkbox" data-change="editPathHop" data-field="joined" data-arg="${id}"${canJoin && h.joined ? ' checked' : ''}${canJoin ? '' : ' disabled'}> One node with hop ${n - 1}</label>` : '<span></span>'}
    </div>
    ${evidence}${suggestion(h)}
    <div class="pe-hop-f">
      <button type="button" class="btn icon sm" data-action="movePathHop" data-arg="${id}" data-dir="-1" aria-label="Move hop ${n} up"${i === 0 ? ' disabled' : ''}>${icon('up')}</button>
      <button type="button" class="btn icon sm pe-down" data-action="movePathHop" data-arg="${id}" data-dir="1" aria-label="Move hop ${n} down"${i === hops.length - 1 ? ' disabled' : ''}>${icon('up')}</button>
      <button type="button" class="btn sm outline" data-action="testPathHop" data-arg="${id}">${icon('activity')}Test</button>
      <span class="pe-test" role="status" aria-live="polite">${tests.get(h.id) || ''}</span>
      <button type="button" class="btn sm danger pe-remove" data-action="removePathHop" data-arg="${id}" aria-label="Remove hop ${n}">${icon('close')}Remove</button>
    </div></li>`;
}

function renderRemoved() {
  const box = $('pathEditorRemoved');
  box.hidden = !removed.length;
  box.innerHTML = removed.length ? `<h3 class="section-t">Removed when you apply <span class="muted">${removed.length}</span></h3><ul class="pe-gone">${removed.map((r) => {
    const id = escapeHtml(r.hop.id);
    const history = r.live ? `<label class="check"><input type="checkbox" data-change="togglePathHistory" data-arg="${id}"${r.deleteHistory ? ' checked' : ''}> Delete its history too (cannot be undone)</label>` : '';
    return `<li data-removed="${id}"><span class="pe-gone-n"><b>${escapeHtml(r.hop.name)}</b> <span class="mono">${escapeHtml(r.hop.address)}</span></span>${history}<button type="button" class="btn sm" data-action="undoRemovePathHop" data-arg="${id}">Undo</button></li>`;
  }).join('')}</ul>` : '';
}

function render(focus) {
  const hops = draft.hops;
  $('pathEditorHops').innerHTML = hops.length ? hops.map((h, i) => card(h, i, hops)).join('')
    : '<li class="empty">No hops: nothing would be probed. Add one below, or undo a removal.</li>';
  renderRemoved();
  $('pathAddRole').innerHTML = options(ROLES, $('pathAddRole').value || 'isp');
  $('pathAddPosition').innerHTML = options([['end', 'At the end'], ...hops.map((h, i) => [String(i), `Before hop ${i + 1}: ${h.name}`])], 'end');
  const meta = { live: 'nothing changes until Apply', discovery: 'a draft from discovery · nothing changes until Apply', import: 'a draft from a file · nothing changes until Apply' };
  $('pathEditorMeta').textContent = meta[draft.source];
  if (focus) { // the control that was used, on the re-drawn card
    const which = focus.dir ? `[data-dir="${focus.dir}"]` : focus.field ? `[data-field="${focus.field}"]` : '[data-field="name"]';
    const el = document.querySelector(`#pathEditorHops [data-arg="${CSS.escape(focus.id)}"]${which}`);
    if (el && !el.disabled) el.focus();
    else (document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(focus.id)}"] .pe-name`) || $('btnApplyPath')).focus();
  }
}

const hopById = (id) => draft.hops.find((h) => h.id === id);

// An IP literal is probed as it is (its own family); only a host name has a family to pick.
const isName = (address) => !address.includes(':') && !/^[0-9.]+$/.test(address);
const familyOf = (h) => (h.address.includes(':') ? 6 : isName(h.address) ? (h.family || 4) : 4);

// ---------- opening ----------
async function open(arg = '') {
  note('', '');
  openModal('pathEditorModal');
  $('pathEditorHops').innerHTML = '<li class="skel-stack" aria-busy="true"><div class="skel line" style="width:90%"></div><div class="skel line" style="width:70%"></div></li>';
  try {
    const path = await fetchPath();
    if (arg.startsWith('discovery:')) {
      const res = await fetch(`/api/discovery/${encodeURIComponent(arg.slice(10))}`);
      if (!res.ok) throw new Error(await errorText(res));
      showDiscovery(path, await res.json());
    } else {
      load(path, path.hops, 'live');
    }
  } catch (e) {
    note('crit', `<b>The path could not be loaded.</b> ${escapeHtml(e.message)}`);
  }
}

function showDiscovery(path, run) {
  if (run.state !== 'done' || !run.draft) {
    note('crit', `<b>Discovery did not finish.</b> ${escapeHtml(run.progress || '')} ${(run.notes || []).map(escapeHtml).join(' ')}`);
    if (!draft) load(path, path.hops, 'live');
    return;
  }
  load(path, run.draft.hops || [], 'discovery', run.notes || []);
  const counts = ['new', 'changed', 'gone'].map((s) => [s, draft.hops.filter((h) => h.status === s).length]).filter(([, c]) => c);
  const diff = counts.length ? ` Compared with the live path: ${counts.map(([s, c]) => `${c} ${s === 'gone' ? 'not found' : s}`).join(', ')}.` : '';
  const notes = (run.notes || []).length ? `<ul class="pe-notes">${run.notes.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>` : '';
  note('unknown', `<b>Discovery found ${draft.hops.length} hop${draft.hops.length === 1 ? '' : 's'}.</b> Check each one's role and reasons, then Apply.${diff}${notes}`);
}

async function rediscover() {
  if (!draft) return;
  const button = $('btnRediscover');
  button.disabled = true;
  note('unknown', 'Starting discovery…');
  try {
    const id = await startDiscovery({ asnLookup: $('pathDiscAsn').checked, snmp: $('pathDiscSnmp').checked });
    const run = await waitDiscovery(id, (text) => note('unknown', `${escapeHtml(text)}`));
    showDiscovery(await fetchPath(), run);
  } catch (e) {
    note(e.unavailable ? 'warn' : 'crit', `<b>Discovery did not run.</b> ${escapeHtml(e.message)}`);
  } finally {
    button.disabled = false;
  }
}

// ---------- edits ----------
function edit(el, ev) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  const field = el.dataset.field;
  if (field === 'name') { // no re-draw: typing (and Tab) keep the focus; only the "Where" list names the hops
    h.name = el.value;
    const opt = $('pathAddPosition').querySelector(`option[value="${draft.hops.indexOf(h)}"]`);
    if (opt) opt.textContent = `Before hop ${draft.hops.indexOf(h) + 1}: ${h.name}`;
    return;
  }
  if (field === 'category') h.category = el.value;
  else if (field === 'ignores_ping') h.ignores_ping = el.value === 'yes' ? true : el.value === 'no' ? false : null;
  else if (field === 'device_id') h.device_id = el.value || null;
  else if (field === 'family') { h.family = Number(el.value) === 6 ? 6 : 4; tests.delete(h.id); }
  else if (field === 'joined') h.joined = el.checked;
  render({ id: h.id, field });
}

function move(el) {
  const i = draft.hops.findIndex((h) => h.id === el.dataset.arg);
  const j = i + Number(el.dataset.dir);
  if (i < 0 || j < 0 || j >= draft.hops.length) return;
  const [h] = draft.hops.splice(i, 1);
  h.joined = false;
  draft.hops.splice(j, 0, h);
  render({ id: h.id, dir: el.dataset.dir });
}

function remove(el) {
  const i = draft.hops.findIndex((h) => h.id === el.dataset.arg);
  if (i < 0) return;
  const [h] = draft.hops.splice(i, 1);
  if (draft.hops[i]) draft.hops[i].joined = draft.hops[i].joined && i > 0;
  removed.push({ hop: h, index: i, live: liveIds.has(h.id), deleteHistory: false });
  render();
  const next = draft.hops[Math.min(i, draft.hops.length - 1)];
  (next ? document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(next.id)}"] .pe-remove`) : document.querySelector('#pathEditorRemoved button'))?.focus();
}

function undo(el) {
  const k = removed.findIndex((r) => r.hop.id === el.dataset.arg);
  if (k < 0) return;
  const [r] = removed.splice(k, 1);
  draft.hops.splice(Math.min(r.index, draft.hops.length), 0, r.hop);
  render({ id: r.hop.id });
}

function addHop(ev) {
  ev.preventDefault();
  const address = $('pathAddAddress').value.trim();
  const msg = hostProblem(address);
  const hint = $('pathAddMsg');
  $('pathAddForm').classList.toggle('invalid', !!msg);
  if (msg) { hint.textContent = msg; $('pathAddAddress').focus(); return; }
  if (draft.hops.length >= 64) { hint.textContent = 'A path has at most 64 hops.'; return; }
  const category = $('pathAddRole').value;
  const where = $('pathAddPosition').value;
  const at = where === 'end' ? draft.hops.length : Number(where);
  const family = isName(address) && $('pathAddFamily').value === '6' ? 6 : 4;
  const hop = { id: freshId(category, address), name: address, address, family, category, enabled: true, record_type: 'A', nameserver: null, color: null,
    group: null, ignores_ping: null, device_id: null, joined: false, added: true };
  draft.hops.splice(at, 0, hop);
  $('pathAddAddress').value = '';
  hint.textContent = 'Added to the draft. Test it, then Apply.';
  render({ id: hop.id });
}

async function test(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  el.disabled = true;
  const out = el.parentElement.querySelector('.pe-test');
  out.textContent = 'Testing…';
  let html;
  try {
    const res = await fetch('/api/path/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: h.address, family: isName(h.address) ? (h.family || 4) : 4 }) });
    if (!res.ok) throw new Error(await errorText(res));
    const r = await res.json();
    const by = escapeHtml(r.responder || 'a router');
    html = r.kind === 'reply' ? `<span class="ok">${icon('ok', 'sm')}Answered in <b class="num">${r.rtt_ms} ms</b></span>`
      : r.kind === 'timeout' ? `<span class="bad">${icon('down', 'sm')}No reply (it may ignore ping)</span>`
        : r.kind === 'ttl_exceeded' ? `<span class="bad">${icon('warn', 'sm')}Time exceeded at ${by}</span>`
          : r.kind === 'unreachable' ? `<span class="bad">${icon('down', 'sm')}Unreachable (reported by ${by})</span>`
            : r.kind === 'ipv6_unavailable' ? `<span class="quiet" title="${escapeHtml(NO_IPV6_WHY)}">${icon('unknown', 'sm')}${escapeHtml(NO_IPV6)}: not probed, not an outage</span>`
            : `<span class="bad">${icon('warn', 'sm')}${escapeHtml(r.error || 'The test failed')}</span>`;
  } catch (e) {
    html = `<span class="bad">${icon('warn', 'sm')}${escapeHtml(e.message)}</span>`;
  }
  tests.set(h.id, html);
  out.innerHTML = html;
  el.disabled = false;
}

function connect(el) {
  const h = hopById(el.dataset.arg);
  if (h) h.device_id = el.dataset.device;
  render();
  // the device setup dialog's own action (dispatch.js), so this folder imports no other; it opens over the editor
  // (dialog.css) and the draft stays as it is
  const setup = document.createElement('button');
  setup.type = 'button';
  setup.hidden = true;
  setup.dataset.action = 'openDeviceSetup';
  setup.dataset.arg = el.dataset.device;
  document.body.appendChild(setup);
  setup.click();
  setup.remove();
}

async function apply() {
  if (!draft || applying) return;
  applying = true;
  const button = $('btnApplyPath');
  const label = button.querySelector('span');
  button.disabled = true;
  label.textContent = 'Applying…';
  try {
    const res = await fetch('/api/path', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_revision: draft.base, hops: payload() }) });
    if (res.status === 409) {
      note('crit', '<b>The path was changed elsewhere</b> (another tab or another device) after this editor opened, so nothing was applied. '
        + '<button type="button" class="btn sm outline" data-action="reloadPathEditor">Load the current path</button>');
      return;
    }
    if (!res.ok) { note('crit', `<b>Nothing was applied.</b> ${escapeHtml(await errorText(res))}`); return; }
    const failed = [];
    for (const r of removed.filter((x) => x.live && x.deleteHistory)) {
      const del = await fetch(`/api/path/history/${encodeURIComponent(r.hop.id)}`, { method: 'DELETE' });
      if (!del.ok) failed.push(r.hop.name);
    }
    if (failed.length) { note('warn', `<b>Applied</b>, but the history of ${escapeHtml(failed.join(', '))} could not be deleted.`); return; }
    note('good', '<b>Applied.</b> The new path is probed from now on; the dashboard reloads to show it.');
    setTimeout(() => location.reload(), 600); // every panel then draws the new path from the start
  } catch (e) {
    note('crit', `<b>Nothing was applied.</b> ${escapeHtml(e.message)}`);
  } finally {
    applying = false;
    button.disabled = false;
    label.textContent = 'Apply';
  }
}

async function importFile(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  try {
    const res = await fetch('/api/path/import', { method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: await file.text() });
    if (!res.ok) throw new Error(await errorText(res));
    const { hops } = await res.json();
    const path = await fetchPath();
    load(path, hops, 'import');
    note('unknown', `<b>Read ${hops.length} hop${hops.length === 1 ? '' : 's'} from ${escapeHtml(file.name)}.</b> Review them, then Apply.`);
  } catch (e) {
    note('crit', `<b>The file was not imported.</b> ${escapeHtml(e.message)}`);
  }
}

function cancel() {
  draft = null;
  removed = [];
  closeModal('pathEditorModal');
}

registerActions('click', {
  openPathEditor: (el) => open(el.dataset.arg || ''),
  cancelPathEditor: () => cancel(),
  reloadPathEditor: () => open(''),
  movePathHop: (el) => move(el),
  removePathHop: (el) => remove(el),
  undoRemovePathHop: (el) => undo(el),
  testPathHop: (el) => test(el),
  connectPathDevice: (el) => connect(el),
  applyPath: () => apply(),
  rediscoverPath: () => rediscover(),
  importPath: () => $('pathImportFile').click()
});
registerActions('input', { editPathHop: (el, ev) => edit(el, ev) });
registerActions('change', {
  editPathHop: (el, ev) => edit(el, ev),
  togglePathHistory: (el) => { const r = removed.find((x) => x.hop.id === el.dataset.arg); if (r) r.deleteHistory = el.checked; },
  importPathFile: (el) => importFile(el)
});
registerActions('submit', { addPathHop: (el, ev) => addHop(ev) });

// Keyboard: focus moves into the dialog when it opens, Tab stays inside it and Escape closes it.
function keepKeyboard(modal, firstFocus) {
  new MutationObserver(() => {
    if (modal.classList.contains('active') && !modal.contains(document.activeElement)) (modal.querySelector(firstFocus) || modal.querySelector('.m-close')).focus();
  }).observe(modal, { attributes: true, attributeFilter: ['class'] });
  modal.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !ev.defaultPrevented) { closeModal(modal.id); return; }
    if (ev.key !== 'Tab') return;
    const items = [...modal.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')].filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });
}
keepKeyboard($('pathEditorModal'), '.pe-name');
