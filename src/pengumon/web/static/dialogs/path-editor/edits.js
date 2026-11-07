// The path editor's edits of the draft: rename, role, move, merge, remove, add, replace an address, test, device links.

import { cleanName, errorText } from '../../js/path-api.js';
import { NO_IPV6, NO_IPV6_WHY } from '../../js/path.js';
import { state } from '../../js/state.js';
import { escapeHtml, hostProblem, icon } from '../../js/util.js';
import { $, autoLink, freshId, hopById, isName, note, pe } from './draft.js';
import { paintLive, render, renderClean } from './render.js';

// ---------- edits ----------
export function edit(el) {
  if (!pe.draft) return; // a change event that arrives as the dialog closes (Esc in a field)
  const h = hopById(el.dataset.arg);
  if (!h) return;
  const field = el.dataset.field;
  if (field === 'name') { // no re-draw: typing (and Tab) keep the focus; only the "Where" list and the chain name the hops
    h.name = el.value;
    const opt = $('pathAddPosition').querySelector(`option[value="${pe.draft.hops.indexOf(h)}"]`);
    if (opt) opt.textContent = `Before hop ${pe.draft.hops.indexOf(h) + 1}: ${h.name}`;
    document.querySelectorAll(`#pathEditorChain [data-arg="${CSS.escape(h.id)}"] .pe-node-n`).forEach((n) => { n.textContent = h.name; });
    renderClean();
    return;
  }
  if (field === 'warn_latency_ms') { // no re-draw: typing keeps the focus
    const text = el.value.trim();
    const n = Number(text);
    const bad = text !== '' && !(Number.isFinite(n) && n > 0 && n <= 10000);
    h.warn_latency_ms = text === '' ? null : bad ? text : n; // a bad text is sent as typed and the server refuses it
    el.setAttribute('aria-invalid', String(bad));
    const fx = $(`peWarn-${h.id}`);
    if (fx) fx.textContent = bad ? 'Enter a number above 0 and up to 10000.' : 'Only this hop\'s colour. Incidents still use the role\'s level.';
    return;
  }
  if (field === 'category') {
    h.category = el.value;
    h.group = null; // a group belongs to the old role
    h.joined = false;
    autoLink(h);
  } else if (field === 'family') { h.family = Number(el.value) === 6 ? 6 : 4; pe.tests.delete(h.id); }
  render({ id: h.id, field });
}

export function move(el) {
  const i = pe.draft.hops.findIndex((h) => h.id === el.dataset.arg);
  const j = i + Number(el.dataset.dir);
  if (i < 0 || j < 0 || j >= pe.draft.hops.length) return;
  const [h] = pe.draft.hops.splice(i, 1);
  h.joined = false;
  if (pe.draft.hops[i]) pe.draft.hops[i].joined = false; // the hop that took its place no longer follows it
  pe.draft.hops.splice(j, 0, h);
  render({ id: h.id, dir: el.dataset.dir });
}

export function merge(el, joined) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  h.joined = joined;
  render();
  const again = document.querySelector(`#pathEditorChain [data-arg="${CSS.escape(h.id)}"][data-action="${joined ? 'splitPathHops' : 'mergePathHops'}"]`);
  (again || $('pathEditorChain').querySelector('button') || $('btnApplyPath')).focus();
}

export function focusHop(el) {
  const card = document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(el.dataset.arg)}"]`);
  if (!card) return;
  card.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  card.classList.remove('flash');
  void card.offsetWidth; // restarts the highlight
  card.classList.add('flash');
  card.querySelector('.pe-name').focus({ preventScroll: true });
}

export function remove(el) {
  const i = pe.draft.hops.findIndex((h) => h.id === el.dataset.arg);
  if (i < 0) return;
  const [h] = pe.draft.hops.splice(i, 1);
  if (pe.draft.hops[i]) pe.draft.hops[i].joined = pe.draft.hops[i].joined && i > 0;
  pe.removed.push({ hop: h, index: i, live: pe.liveHops.has(h.id), deleteHistory: false });
  render();
  const next = pe.draft.hops[Math.min(i, pe.draft.hops.length - 1)];
  (next ? document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(next.id)}"] .pe-name`) : document.querySelector('#pathEditorRemoved button'))?.focus();
}

export function undo(el) {
  const k = pe.removed.findIndex((r) => r.hop.id === el.dataset.arg);
  if (k < 0) return;
  const [r] = pe.removed.splice(k, 1);
  pe.draft.hops.splice(Math.min(r.index, pe.draft.hops.length), 0, r.hop);
  render({ id: r.hop.id });
}

export function addHop(ev) {
  ev.preventDefault();
  const address = $('pathAddAddress').value.trim();
  const msg = hostProblem(address);
  const hint = $('pathAddMsg');
  $('pathAddForm').classList.toggle('invalid', !!msg);
  if (msg) { hint.textContent = msg; $('pathAddAddress').focus(); return; }
  if (pe.draft.hops.length >= 64) { hint.textContent = 'A path has at most 64 hops.'; return; }
  const category = $('pathAddRole').value;
  const where = $('pathAddPosition').value;
  const at = where === 'end' ? pe.draft.hops.length : Number(where);
  const family = isName(address) && $('pathAddFamily').value === '6' ? 6 : 4;
  const twin = pe.draft.hops.find((h) => h.address.toLowerCase() === address.toLowerCase());
  const hop = { id: freshId(category, address), name: address.slice(0, 100), address, family, category, enabled: true, record_type: 'A', nameserver: null, color: null,
    group: null, device_id: null, joined: false, added: true };
  autoLink(hop);
  pe.draft.hops.splice(at, 0, hop);
  $('pathAddAddress').value = '';
  hint.textContent = twin ? `Added, but ${twin.name} already has this address. Test or preview it, then Apply.` : 'Added to the draft. Test or preview it, then Apply.';
  render({ id: hop.id });
}

export function startAddress(el) {
  pe.addressing = el.dataset.arg;
  render();
  const input = document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(pe.addressing)}"] .pe-addr-f [data-f=address]`);
  if (input) { input.focus(); input.select(); }
}

// Replaces a hop's address in place: same id, so the history stays with it (a renumbered provider router).
export function replaceAddress(form, ev) {
  ev.preventDefault();
  const h = hopById(form.dataset.arg);
  if (!h) return;
  const input = form.querySelector('[data-f=address]');
  const address = input.value.trim();
  const msg = hostProblem(address);
  if (msg) {
    form.classList.add('invalid');
    form.querySelector('[data-f=why]').textContent = msg;
    input.focus();
    return;
  }
  if (address !== h.address) {
    const was = pe.liveHops.get(h.id);
    h.addressFrom = was && was.address !== address ? was.address : null;
    h.address = address;
    pe.tests.delete(h.id);
  }
  pe.addressing = null;
  render({ id: h.id, selector: '[data-action=editPathAddress]' });
}

export function cancelAddress(id) {
  pe.addressing = null;
  render({ id, selector: '[data-action=editPathAddress]' });
}

export async function test(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  el.disabled = true;
  pe.tests.set(h.id, 'Testing…');
  paintLive();
  let html;
  try {
    const res = await fetch('/api/path/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: h.address, family: isName(h.address) ? (h.family || 4) : 4 }) });
    if (!res.ok) throw new Error(await errorText(res));
    const r = await res.json();
    const by = escapeHtml(r.responder || 'a router');
    const again = `<button type="button" class="btn sm" data-action="testPathHop" data-needs-edit data-arg="${escapeHtml(h.id)}">Test again</button>`;
    html = (r.kind === 'reply' ? `<span class="ok">${icon('ok', 'sm')}Answered in <b class="num">${r.rtt_ms} ms</b></span>`
      : r.kind === 'timeout' ? `<span class="bad">${icon('down', 'sm')}No reply (it may ignore ping)</span>`
        : r.kind === 'ttl_exceeded' ? `<span class="bad">${icon('warn', 'sm')}Time exceeded at ${by}</span>`
          : r.kind === 'unreachable' ? `<span class="bad">${icon('down', 'sm')}Unreachable (reported by ${by})</span>`
            : r.kind === 'ipv6_unavailable' ? `<span class="quiet" title="${escapeHtml(NO_IPV6_WHY)}">${icon('unknown', 'sm')}${escapeHtml(NO_IPV6)}: not probed, not an outage</span>`
              : `<span class="bad">${icon('warn', 'sm')}${escapeHtml(r.error || 'The test failed')}</span>`) + ` · ${again}`;
  } catch (e) {
    html = `<span class="bad">${icon('warn', 'sm')}${escapeHtml(e.message)}</span>`;
  }
  pe.tests.set(h.id, html);
  paintLive();
}

function openSetup(deviceId) {
  // the device setup dialog's own action (dispatch.js), so this folder imports no other; it opens over the editor
  // (dialog.css) and the draft stays as it is
  const setup = document.createElement('button');
  setup.type = 'button';
  setup.hidden = true;
  setup.dataset.action = 'openDeviceSetup';
  setup.dataset.arg = deviceId;
  document.body.appendChild(setup);
  setup.click();
  setup.remove();
}

// Connect: the device discovery suggests (its driver), or the only one configured, is linked and its setup opens;
// with several to choose from, the chooser opens instead.
export function connect(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  const byDriver = h.suggested_driver ? state.devices.filter((d) => d.driver === h.suggested_driver) : [];
  const single = byDriver.length === 1 ? byDriver[0] : state.devices.length === 1 ? state.devices[0] : null;
  if (!single) {
    pe.picking.add(h.id);
    render({ id: h.id, selector: '[data-change=pickPathDevice]' });
    return;
  }
  h.device_id = single.id;
  h.autoLinked = false;
  h.device_unlinked = false;
  render({ id: h.id, selector: '[data-action=changePathDevice]' });
  if (single.needs_sign_in !== false) openSetup(single.id); // a device with no password has nothing to set up
}

export function pickDevice(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  h.device_id = el.value || null;
  h.autoLinked = false;
  h.device_unlinked = !h.device_id; // "No device" is a choice: no automatic link undoes it
  pe.picking.delete(h.id);
  render({ id: h.id, selector: h.device_id ? '[data-action=changePathDevice]' : '[data-action=connectPathDevice]' });
  const device = state.devices.find((d) => d.id === h.device_id);
  if (device && !device.has_credentials) openSetup(device.id); // not signed in yet: where to do it
}

export function toggleChooser(el) {
  const id = el.dataset.arg;
  if (pe.picking.has(id)) pe.picking.delete(id); else pe.picking.add(id);
  render({ id, selector: pe.picking.has(id) ? '[data-change=pickPathDevice]' : '[data-action=changePathDevice]' });
}

export function cleanNames() {
  let n = 0;
  pe.draft.hops.forEach((h) => { const c = cleanName(h.name); if (c) { h.name = c; n++; } });
  render();
  note('unknown', `<b>Dropped the "Hop N:" prefix from ${n} name${n === 1 ? '' : 's'}.</b> Apply to keep the new names.`);
}
