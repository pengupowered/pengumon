// The path editor's cards and chain: each hop's live measurement (Path health's figures and words), its card, the linked
// device's status, the removed hops and the "Hop N:" offer.

import { bus } from '../../js/bus.js';
import { watchDevice } from '../../js/device-feed.js';
import { cleanName, DEVICE_ROLES, deviceLabel, deviceStatus, ROLE_EFFECT, ROLE_HINT, ROLE_WORD, ROLES } from '../../js/path-api.js';
import { IGNORES_PING, IGNORES_PING_WHY, NO_IPV6, NO_IPV6_WHY, NO_PINGS_WHY, shortLabels } from '../../js/path.js';
import { settingsValues, val } from '../../js/settings-values.js';
import { state } from '../../js/state.js';
import { canEdit, escapeHtml, formatMs, icon, median, STATUS } from '../../js/util.js';
import { $, chainRuns, familyOf, isName, liveTwin, MERGEABLE, ONE_NODE, pe } from './draft.js';
import { guardBack } from './guard.js';

const CONFIDENCE = { high: ['good', 'Sure'], likely: ['unknown', 'Likely'], unknown: ['warn', 'Unsure'] };
const STATUS_CHIP = { new: ['good', 'New'], changed: ['warn', 'Changed'], gone: ['crit', 'Not found by discovery'] };
const LEVEL = { up: 'good', degraded: 'warn', down: 'crit' };
// What a link is, truthfully: the device is read on its own (its card), the link names it on this hop.
const DEVICE_WHY = 'Pengumon also reads this device directly; its details are in the';
const NO_DEVICE_WHY = 'Connect a configured device to see its status here; Pengumon reads it directly and shows its details in the Router or Line panel.';
const ADDRESS_WHY = 'The hop keeps its id and its history (for a router your provider renumbered); its live figures start over at the new address.';
export const PREVIEW_S = 30;

// ---------- the measurement (Path health's figures and words) ----------
const has = (v) => v !== null && v !== undefined;
const fmtLoss = (v) => `${+(+v || 0).toFixed(1)}%`;
const LAST_MINUTE = '<span class="muted">(last minute)</span>';

function figureOf(t) {
  const recent = (t.recent_latencies || []).slice(-60);
  const ms = recent.length ? median(recent) : (has(t.last_ms) ? t.last_ms : null);
  const loss = has(t.loss_recent_pct) ? t.loss_recent_pct : t.loss_pct;
  return { ms, loss };
}

// {level, html, title, ms}: what the card's measurement line and the chain's node say.
function measurement(h) {
  if (h.enabled === false) return { level: 'unknown', html: 'Paused · not probed', word: 'Paused' };
  const twin = liveTwin(h);
  if (twin) {
    const t = pe.live.get(twin);
    if (t.enabled !== false && t.probing_unavailable === true) return { level: 'unknown', html: 'Not probed · pings are not allowed', title: NO_PINGS_WHY, word: 'Not probed' };
    if (t.enabled !== false && t.ipv6_unavailable === true) return { level: 'unknown', html: escapeHtml(NO_IPV6), title: NO_IPV6_WHY, word: 'Not probed' };
    if (t.enabled !== false && t.ignores_ping === true) {
      return { level: 'unknown', html: `No reply · ${escapeHtml(IGNORES_PING.toLowerCase())} (not counted as an outage)`, title: IGNORES_PING_WHY, word: 'No reply' };
    }
    if (!t.sent) return { level: 'unknown', html: 'Waiting for the first probe', word: '…' };
    const { ms, loss } = figureOf(t);
    if (t.status === 'down' || !has(ms)) {
      return { level: 'crit', html: `<b>Not answering</b> · <span class="num">${fmtLoss(loss)}</span> loss ${LAST_MINUTE}`, word: 'No reply' };
    }
    const level = LEVEL[t.status] || 'unknown';
    return { level, html: `<b class="num">${formatMs(ms)} ms</b> · <span class="num">${fmtLoss(loss)}</span> loss ${LAST_MINUTE}`, ms };
  }
  const p = pe.preview && pe.preview.hops[h.id] && pe.preview.addresses[h.id] === h.address ? pe.preview.hops[h.id] : null;
  if (p) {
    const when = pe.preview.state === 'done' ? `<span class="muted">(preview, ${PREVIEW_S} s)</span>` : `<span class="muted">(previewing, ${Math.round(pe.preview.elapsed || 0)} s)</span>`;
    if (p.kind === 'ipv6_unavailable') return { level: 'unknown', html: `${escapeHtml(NO_IPV6)} ${when}`, title: NO_IPV6_WHY, word: 'Not probed' };
    if (!p.received) {
      if (p.kind === 'unreachable') return { level: 'crit', html: `<b>Unreachable</b> ${when}`, title: 'A router on the way says this address cannot be reached.', word: 'Unreachable' };
      return { level: p.sent ? 'warn' : 'unknown', html: `${p.sent ? 'No reply' : 'Starting'} ${when}`, title: p.sent ? 'No answer yet: it may ignore ping, or be down. Once it is on the path, Pengumon tells the two apart.' : '', word: 'No reply' };
    }
    const level = p.loss_pct > 0 ? 'warn' : 'good';
    return { level, html: `<b class="num">${formatMs(p.median_ms)} ms</b> · <span class="num">${fmtLoss(p.loss_pct)}</span> loss ${when}`, ms: p.median_ms };
  }
  if (pe.tests.has(h.id)) return { level: 'unknown', html: pe.tests.get(h.id), test: true, word: 'Tested' };
  return {
    level: 'unknown', word: 'Not probed',
    html: `Not probed yet · <button type="button" class="btn sm outline" data-action="testPathHop" data-needs-edit data-arg="${escapeHtml(h.id)}">${icon('activity')}Test</button>`
  };
}

function liveMarkup(m) {
  return `${m.test ? '' : icon(STATUS[m.level].icon, 'sm')}<span>${m.html}</span>`;
}

// ---------- rendering ----------
const options = (pairs, value) => pairs.map(([v, w]) => `<option value="${escapeHtml(v)}"${v === value ? ' selected' : ''}>${escapeHtml(w)}</option>`).join('');

function labelsOf(hops) {
  return shortLabels(hops.map((h) => ({ id: h.id, category: h.category, name: h.name, address: h.address, enabled: h.enabled })));
}

// The chain: this host, then one node per run of hops Path health draws as one (chainRuns), each with the link before
// it (a step: the chain wraps between steps). A link between two provider hops or two public servers carries Merge, a
// merged node Split between its members.
function renderChain() {
  const hops = pe.draft.hops;
  const labels = labelsOf(hops);
  const list = chainRuns(hops);
  const parts = [`<li class="pe-origin">${icon('host')}<b>This host</b></li>`];
  list.forEach((run, k) => {
    const prev = k ? list[k - 1][list[k - 1].length - 1] : null;
    const first = run[0];
    const mergeable = prev && prev.category === first.category && MERGEABLE.includes(first.category);
    const link = `<span class="pe-link"><span class="pe-wire"></span>${mergeable
      ? `<button type="button" class="btn sm pe-merge" data-action="mergePathHops" data-needs-edit data-arg="${escapeHtml(first.id)}" title="Show ${escapeHtml(prev.name)} and ${escapeHtml(first.name)} as one node on Path health">Merge</button>` : ''}</span>`;
    const members = run.map((h, i) => {
      const m = measurement(h);
      const split = i && MERGEABLE.includes(h.category)
        ? `<button type="button" class="btn sm pe-split" data-action="splitPathHops" data-needs-edit data-arg="${escapeHtml(h.id)}" title="Show ${escapeHtml(h.name)} as a node of its own">Split</button>` : '';
      return `${split}<button type="button" class="pe-node-m" data-action="focusPathHop" data-arg="${escapeHtml(h.id)}" title="${escapeHtml(`${h.name} · ${h.address}. Edit it below`)}">
        <span class="pe-node-k">${escapeHtml(labels[h.id] || ROLE_WORD[h.category] || h.category)}</span><span class="pe-node-n">${escapeHtml(h.name)}</span>
        <span class="pe-node-v st-${m.level}" data-chain="${escapeHtml(h.id)}">${chainValue(m)}</span></button>`;
    }).join('');
    const off = !ONE_NODE.includes(first.category) && !MERGEABLE.includes(first.category);
    parts.push(`<li class="pe-step">${link}<span class="pe-node${run.length > 1 ? ' merged' : ''}${off ? ' off' : ''}"${off ? ' title="Not a stage of the path: Path health lists it under the chain"' : ''}>${members}</span></li>`);
  });
  if (!hops.length) parts.push('<li class="pe-step"><span class="pe-link"><span class="pe-wire"></span></span><span class="pe-node empty">No hops</span></li>');
  $('pathEditorChain').innerHTML = parts.join('');
}

const chainValue = (m) => (has(m.ms) ? `<b class="num">${formatMs(m.ms)}</b> ms` : escapeHtml(m.word || ''));

// The device a home hop is: a sentence, with Change (linked) or Connect (none) and, while open, a chooser.
// The device's own status, as its card says it, from its latest reading: "Healthy · CPU 12% · 23 clients".
function deviceNow(device) {
  const st = deviceStatus(device, bus.last(`device:${device.id}`));
  return `<span class="pe-dev-st st-${st.level}">${icon(STATUS[st.level].icon, 'sm')}${escapeHtml(st.word || 'Waiting')}</span>${st.figures.map((f) => ` · <span class="num">${escapeHtml(f)}</span>`).join('')}`;
}

function deviceBlock(h) {
  if (!DEVICE_ROLES.includes(h.category)) return '';
  const id = escapeHtml(h.id);
  const device = h.device_id ? state.devices.find((d) => d.id === h.device_id) : null;
  const choices = [['', 'No device'], ...state.devices.map((d) => [d.id, deviceLabel(d)])];
  if (h.device_id && !device) choices.push([h.device_id, `${h.device_id} (not configured here)`]);
  const chooser = pe.picking.has(h.id)
    ? `<label class="field pe-dev-pick"><span>Device</span><select class="input" data-change="pickPathDevice" data-arg="${id}" aria-label="Device of ${escapeHtml(h.name)}">${options(choices, h.device_id || '')}</select></label>` : '';
  let line;
  let why = NO_DEVICE_WHY;
  if (h.device_id) {
    const auto = h.autoLinked ? ' <span class="muted">· linked by its address</span>' : '';
    const now = device ? ` · <span class="pe-dev-now" data-dev-now="${escapeHtml(device.id)}">${deviceNow(device)}</span>` : ' · <span class="muted">not configured on this install</span>';
    line = `${icon(device ? 'plug' : 'warn', 'sm')}<span>Readings from: <b class="pe-dev-name">${escapeHtml(device ? deviceLabel(device) : h.device_id)}</b>${now}${auto}</span>`
      + `<button type="button" class="btn sm" data-action="changePathDevice" data-arg="${id}" aria-expanded="${pe.picking.has(h.id)}">Change</button>`;
    if (device) {
      const panel = deviceStatus(device, null);
      why = `${escapeHtml(DEVICE_WHY)} <button type="button" class="pe-lnk" data-action="showDevicePanel" data-arg="${escapeHtml(panel.panel)}">${escapeHtml(panel.panelName)} panel</button>.`;
    } else {
      why = 'That device is not set up on this install (Settings, Devices): nothing reads it here.';
    }
  } else if (state.devices.length) {
    line = `${icon('plug', 'sm')}<span class="muted">No device connected</span> · <button type="button" class="btn sm outline" data-action="connectPathDevice" data-needs-edit data-arg="${id}">${icon('lock')}Connect</button>`;
  } else {
    line = `${icon('plug', 'sm')}<span class="muted">No device connected</span> · <span class="fine">add one in Settings, Devices, to read it</span>`;
  }
  const kindKnown = state.devices.find((d) => d.driver === h.suggested_driver);
  const driver = !h.device_id && h.suggested_driver
    ? `<p class="fine pe-suggest">${kindKnown ? `Discovery thinks this is a device Pengumon can read: ${escapeHtml(kindKnown.kind || 'one you have set up')}.` : 'Discovery thinks this is a device Pengumon can read: add it in Settings, Devices.'}</p>` : '';
  return `<div class="pe-dev"><div class="pe-dev-l">${line}</div>${chooser}<p class="fine pe-dev-why">${why}</p>${driver}</div>`;
}

// The readings of the linked devices, followed while the editor is open (the device cards' own feed).
function watchDevices() {
  if (!pe.draft) return;
  const want = new Set(pe.draft.hops.filter((h) => DEVICE_ROLES.includes(h.category) && h.device_id).map((h) => h.device_id));
  state.devices.filter((d) => want.has(d.id) && !pe.deviceWatch.has(d.id)).forEach((d) => {
    pe.deviceWatch.set(d.id, watchDevice(bus, d, () => paintDevice(d)));
  });
}

function paintDevice(device) {
  const html = deviceNow(device);
  document.querySelectorAll(`#pathEditorHops [data-dev-now="${CSS.escape(device.id)}"]`).forEach((el) => {
    if (el.innerHTML !== html) el.innerHTML = html;
  });
}

export function unwatchDevices() {
  pe.deviceWatch.forEach((off) => off());
  pe.deviceWatch = new Map();
}

function addressForm(h) {
  if (pe.addressing !== h.id) return '';
  const id = escapeHtml(h.id);
  return `<form class="pe-addr-f" data-submit="replacePathAddress" data-arg="${id}" novalidate>
    <label class="field grow"><span>New address of ${escapeHtml(h.name)}</span><input class="input mono" type="text" value="${escapeHtml(h.address)}" maxlength="255" autocomplete="off" autocapitalize="off" spellcheck="false" data-f="address" aria-describedby="peAddrWhy-${id}"></label>
    <button type="submit" class="btn sm primary">Replace</button><button type="button" class="btn sm" data-action="cancelPathAddress" data-arg="${id}">Cancel</button>
    <p class="fine" id="peAddrWhy-${id}" data-f="why">${escapeHtml(ADDRESS_WHY)}</p></form>`;
}

// The role's amber level for latency (Settings > Outage rules > Advanced > Hop colours), the placeholder of "Warn above".
const ROLE_LEVEL = { local: 'local_latency_ms', gateway: 'gateway_latency_ms', public: 'public_latency_ms', dns: 'dns_latency_ms' };
function roleLevelText(h) {
  const key = ROLE_LEVEL[h.category];
  const ms = key ? val(settingsValues(), `prober.hop_status.${key}`, null) : null;
  return ms === null ? 'role level: none' : `role level: ${ms} ms`;
}

// "Warn above [ ] ms": this hop turns amber above its own average latency instead of its role's. Colour only: incidents
// still use the role's level.
function moreBlock(h, n) {
  const id = escapeHtml(h.id);
  const set = h.warn_latency_ms !== null && h.warn_latency_ms !== undefined && h.warn_latency_ms !== '';
  return `<details class="pe-more" data-more="${id}"${h.moreOpen || set ? ' open' : ''}><summary>More</summary>
    <label class="field pe-warn"><span>Warn above</span><span class="pe-warn-in"><input class="input num" type="text" inputmode="decimal" autocomplete="off" value="${set ? escapeHtml(h.warn_latency_ms) : ''}" placeholder="${escapeHtml(roleLevelText(h))}" maxlength="8"${canEdit() ? '' : ' disabled'} data-input="editPathHop" data-change="editPathHop" data-field="warn_latency_ms" data-arg="${id}" aria-label="Warn above, in milliseconds, for hop ${n}" aria-describedby="peWarn-${id}"><span class="muted">ms</span></span></label>
    <p class="fine pe-warn-fx" id="peWarn-${id}">Only this hop's colour. Incidents still use the role's level.</p></details>`;
}

function card(h, i, hops) {
  const n = i + 1;
  const id = escapeHtml(h.id);
  const chips = [];
  if (h.confidence && CONFIDENCE[h.confidence]) {
    const [level, word] = CONFIDENCE[h.confidence];
    chips.push(`<span class="chip st st-${level}" title="How sure discovery is of this hop's role">${escapeHtml(word)}</span>`);
  }
  if (STATUS_CHIP[h.status]) chips.push(`<span class="chip st st-${STATUS_CHIP[h.status][0]}">${STATUS_CHIP[h.status][1]}</span>`);
  if (h.added) chips.push('<span class="chip st st-good">Added</span>');
  if (h.addressFrom) chips.push(`<span class="chip st st-warn" title="${escapeHtml(`Was ${h.addressFrom}; the history stays with the hop`)}">New address</span>`);
  if (h.enabled === false) chips.push('<span class="chip">Paused</span>');
  if (familyOf(h) === 6) chips.push('<span class="chip">IPv6</span>');
  const run = chainRuns(hops).find((r) => r.includes(h));
  const node = run.length > 1 ? `<span class="pe-hint">one node with ${escapeHtml(run.filter((o) => o !== h).map((o) => o.name).join(', '))}</span>` : '';
  const evidence = (h.evidence || []).length
    ? `<ul class="pe-ev" aria-label="Why discovery thinks so">${h.evidence.map((e) => `<li>${escapeHtml(e.text)}</li>`).join('')}</ul>` : '';
  const m = measurement(h);
  return `<li class="pe-hop${h.status === 'gone' ? ' gone' : ''}" data-hop="${id}">
    <div class="pe-hop-h"><span class="pe-n num" aria-hidden="true">${n}</span>
      <input class="input pe-name" type="text" value="${escapeHtml(h.name)}" maxlength="100" data-input="editPathHop" data-change="editPathHop" data-field="name" data-arg="${id}" aria-label="Name of hop ${n}">
      <span class="pe-live st-${m.level}" data-live="${id}"${m.title ? ` title="${escapeHtml(m.title)}"` : ''}>${liveMarkup(m)}</span></div>
    <div class="pe-hop-sub"><span class="mono pe-addr" title="${escapeHtml(h.address)}">${escapeHtml(h.address)}</span><button type="button" class="pe-lnk" data-action="editPathAddress" data-needs-edit data-arg="${id}" title="${escapeHtml(ADDRESS_WHY)}">Change address</button>
      <span class="pe-hint">${escapeHtml(ROLE_WORD[h.category] || h.category)}: ${escapeHtml(ROLE_HINT[h.category] || '')}</span>${node}${chips.join('')}</div>
    ${addressForm(h)}
    <div class="pe-hop-b">
      <label class="field"><span>Role</span><select class="input" data-change="editPathHop" data-field="category" data-arg="${id}" aria-label="Role of hop ${n}" aria-describedby="peFx-${id}">${options(ROLES, h.category)}</select></label>
      ${isName(h.address) ? `<label class="field"><span>Probe the name's</span><select class="input" data-change="editPathHop" data-field="family" data-arg="${id}" aria-label="Address family of hop ${n}">${options([['4', 'IPv4 address'], ['6', 'IPv6 address']], String(h.family || 4))}</select></label>` : ''}
      <div class="pe-hop-f">
        <button type="button" class="btn icon sm" data-action="movePathHop" data-needs-edit data-arg="${id}" data-dir="-1" aria-label="Move hop ${n} up"${i === 0 ? ' disabled' : ''}>${icon('up')}</button>
        <button type="button" class="btn icon sm pe-down" data-action="movePathHop" data-needs-edit data-arg="${id}" data-dir="1" aria-label="Move hop ${n} down"${i === hops.length - 1 ? ' disabled' : ''}>${icon('up')}</button>
        <button type="button" class="btn sm danger pe-remove" data-action="removePathHop" data-needs-edit data-arg="${id}" aria-label="Remove hop ${n}">${icon('close')}Remove</button>
      </div>
    </div>
    <p class="fine pe-role-fx" id="peFx-${id}">${escapeHtml(ROLE_EFFECT[h.category] || '')}</p>
    ${moreBlock(h, n)}${deviceBlock(h)}${evidence}</li>`;
}

function renderRemoved() {
  const box = $('pathEditorRemoved');
  box.hidden = !pe.removed.length;
  box.innerHTML = pe.removed.length ? `<h3 class="section-t">Removed when you apply <span class="muted">${pe.removed.length}</span></h3><ul class="pe-gone">${pe.removed.map((r) => {
    const id = escapeHtml(r.hop.id);
    const history = r.live ? `<label class="check"><input type="checkbox" data-change="togglePathHistory" data-arg="${id}"${r.deleteHistory ? ' checked' : ''}> Delete its history too (cannot be undone)</label>` : '';
    return `<li data-removed="${id}"><span class="pe-gone-n"><b>${escapeHtml(r.hop.name)}</b> <span class="mono">${escapeHtml(r.hop.address)}</span></span>${history}<button type="button" class="btn sm" data-action="undoRemovePathHop" data-needs-edit data-arg="${id}">Undo</button></li>`;
  }).join('')}</ul>` : '';
}

// The names with a "Hop N:" prefix (older configs numbered them; Path health numbers the hops itself): one offer.
export function renderClean() {
  const dirty = pe.draft.hops.filter((h) => cleanName(h.name));
  $('pathEditorClean').innerHTML = dirty.length ? `<div class="result st-unknown pe-clean">${icon('info')}<span><b>${dirty.length} name${dirty.length === 1 ? ' starts' : 's start'} with "Hop N:"</b> (${escapeHtml(dirty.slice(0, 2).map((h) => `"${h.name}"`).join(', '))}${dirty.length > 2 ? ', …' : ''}). Path health numbers the hops itself.</span>`
    + '<button type="button" class="btn sm outline" data-action="cleanPathNames" data-needs-edit>Drop the prefixes</button></div>' : '';
}

export function render(focus) {
  const hops = pe.draft.hops;
  guardBack();
  renderChain();
  renderClean();
  $('pathEditorHops').innerHTML = hops.length ? hops.map((h, i) => card(h, i, hops)).join('')
    : '<li class="empty">No hops: nothing would be probed. Add one below, or undo a removal.</li>';
  renderRemoved();
  watchDevices();
  $('pathAddRole').innerHTML = options(ROLES, $('pathAddRole').value || 'isp');
  $('pathAddRoleFx').textContent = ROLE_EFFECT[$('pathAddRole').value] || '';
  $('pathAddPosition').innerHTML = options([['end', 'At the end'], ...hops.map((h, i) => [String(i), `Before hop ${i + 1}: ${h.name}`])], 'end');
  const meta = {
    live: 'nothing changes until Apply', discovery: 'a draft from discovery · nothing changes until Apply',
    import: 'a draft from a file · nothing changes until Apply', version: `revision ${pe.draft.revision} as a draft · nothing changes until Apply`
  };
  $('pathEditorMeta').textContent = meta[pe.draft.source];
  permissions();
  if (focus) { // the control that was used, on the re-drawn card
    const which = focus.dir ? `[data-dir="${focus.dir}"]` : focus.field ? `[data-field="${focus.field}"]` : focus.selector || '[data-field="name"]';
    const el = document.querySelector(`#pathEditorHops [data-arg="${CSS.escape(focus.id)}"]${which}`) || document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(focus.id)}"] ${which}`);
    if (el && !el.disabled) el.focus();
    else (document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(focus.id)}"] .pe-name`) || $('btnApplyPath')).focus();
  }
}

// Without the right to edit (a viewer who has not signed in): look, but no Apply, Rediscover, Preview or Restore.
export function permissions() {
  const ok = canEdit();
  $('pathEditorReadOnly').hidden = ok;
  // the tooltip and the blocked click are js/util.js's (data-needs-edit); these are off outright as well
  ['btnApplyPath', 'btnRediscover', 'btnPreviewPath'].forEach((b) => { $(b).disabled = !ok || (b === 'btnApplyPath' && pe.applying); });
  document.querySelectorAll('#pathVersionList [data-action=loadPathVersion]').forEach((b) => { b.disabled = !ok; });
}

// The live measurement of every card and chain node, in place (typing in a name field keeps its focus).
export function paintLive() {
  if (!pe.draft) return;
  pe.draft.hops.forEach((h) => {
    const m = measurement(h);
    const el = document.querySelector(`#pathEditorHops [data-live="${CSS.escape(h.id)}"]`);
    if (el) {
      const html = liveMarkup(m);
      if (el.dataset.html !== html) {
        el.dataset.html = html;
        el.innerHTML = html;
        el.className = `pe-live st-${m.level}`;
        if (m.title) el.title = m.title; else el.removeAttribute('title');
      }
    }
    const node = document.querySelector(`#pathEditorChain [data-chain="${CSS.escape(h.id)}"]`);
    if (node) {
      const v = chainValue(m);
      if (node.innerHTML !== v) node.innerHTML = v;
      node.className = `pe-node-v st-${m.level}`;
    }
  });
}

export function onTargets(targets) {
  pe.live = new Map((targets || []).map((t) => [t.target_id, t]));
  paintLive();
}
