// The path editor: the probe path as a draft of hop cards in path order (GET /api/path), edited here and applied in
// one PUT /api/path; nothing goes live until Apply. Opened by Path health's Edit button, by its first-run banner and its
// "Your path changed" notice (with a discovery's draft, data-arg "discovery:<id>") and by Rediscover inside it.
//
// At the top, the draft as Path health draws it: one node per hop (or per merged run of hops), each with what the
// prober measures now; a click on a node goes to its card, and Merge / Split on the links make two neighbouring hops of
// one role one node. Every card shows its live measurement from the `targets` topic (the same figures as Path health:
// the median of the last 60 probes and the loss over them), updated while the editor is open; a hop that is not probed
// yet says so, with Test (one probe) and Preview (30 s of probes, POST /api/path/preview) to measure it before Apply.
// Whether a hop ignores ping is observed by the server (engine/path_rules.py), never set here. A hop of your network or
// the line can be the configured device whose readings Pengumon shows: linked automatically when one device is at its
// address (GET /api/path's device_matches, engine/device_links.py), changed by hand. A removed hop stops being probed;
// its history is kept (hidden) unless "delete its history" is ticked. A draft from discovery shows each hop's
// confidence, its reasons (evidence), a suggested device driver and the difference from the live path. Earlier
// applied paths (GET /api/path/versions) load as a draft to restore.

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { state } from '../../js/state.js';
import * as util from '../../js/util.js';
import {
  DEVICE_ROLES, ROLES, ROLE_EFFECT, ROLE_HINT, ROLE_WORD, cleanName, deviceHost, deviceLabel, deviceStatus, errorText, fetchPath,
  fetchVersion, fetchVersions, readPreview, startDiscovery, startPreview, stopPreview, waitDiscovery
} from '../../js/path-api.js';
import { IGNORES_PING, IGNORES_PING_WHY, NO_IPV6, NO_IPV6_WHY, median, shortLabels } from '../../js/path.js';
import { watchDevice } from '../../js/device-feed.js';

const { closeModal, escapeHtml, hostProblem, icon, openModal, STATUS } = util;
// The sign-in helpers (js/util.js): canEdit(), and data-needs-edit on every control that changes something (dimmed with
// the "View only" tooltip, its clicks blocked, for a viewer who has not signed in).
const { canEdit } = util;

const $ = (id) => document.getElementById(id);
const UI_FIELDS = ['joined', 'added', 'evidence', 'confidence', 'suggested_driver', 'status', 'addressFrom', 'autoLinked'];
const CONFIDENCE = { high: ['good', 'Sure'], likely: ['unknown', 'Likely'], unknown: ['warn', 'Unsure'] };
const STATUS_CHIP = { new: ['good', 'New'], changed: ['warn', 'Changed'], gone: ['crit', 'Not found by discovery'] };
const LEVEL = { up: 'good', degraded: 'warn', down: 'crit' };
// What a link is, truthfully: the device is read on its own (its card), the link names it on this hop.
const DEVICE_WHY = 'Pengumon also reads this device directly; its details are in the';
const NO_DEVICE_WHY = 'Connect a configured device to see its status here; Pengumon reads it directly and shows its details in the Router or Line panel.';
const ADDRESS_WHY = 'The hop keeps its id and its history (for a router your provider renumbered); its live figures start over at the new address.';
const PREVIEW_S = 30;

let draft = null; // {base, hops, source: 'live' | 'discovery' | 'import' | 'version', notes, revision}
let removed = []; // [{hop, index, live, deleteHistory}]
let liveHops = new Map(); // id -> the hop as it is live (GET /api/path), to tell a hop probed now from a new or changed one
let tests = new Map(); // hop id -> its last test result, as markup
let picking = new Set(); // hop ids whose device chooser is open
let addressing = null; // the hop id whose address is being replaced
let live = new Map(); // target id -> the live topic's target (prober statistics)
let preview = null; // {id, state, elapsed, hops: {id: result}, addresses: {id: address}}
let unsubscribe = null;
let deviceWatch = new Map(); // device id -> unsubscribe from its readings (js/device-feed.js), while the editor is open
let applying = false;

function note(level, html) {
  $('pathEditorNote').innerHTML = html ? `<div class="result st-${level}">${icon(level === 'good' ? 'ok' : level === 'crit' ? 'down' : level === 'warn' ? 'warn' : 'unknown')}<span>${html}</span></div>` : '';
}

// ---------- the draft ----------
function load(path, hops, source, { notes = [], matches = {}, revision = null } = {}) {
  draft = { base: path.revision, source, notes, revision, hops: hops.slice().sort((a, b) => a.position - b.position).map((h) => ({ ...h })) };
  draft.hops.forEach((h, i) => {
    delete h.ignores_ping; // observed, never set (an older export or draft may still carry it)
    const prev = draft.hops[i - 1];
    // public servers without a group are one node on Path health ("internet"), as are those of one group
    h.joined = !!prev && (h.category === 'public' && prev.category === 'public'
      ? (h.group || 'internet') === (prev.group || 'internet') : !!h.group && h.group === prev.group);
  });
  liveHops = new Map(path.hops.map((h) => [h.id, h]));
  removed = [];
  tests = new Map();
  picking = new Set();
  addressing = null;
  if (source === 'import' || source === 'version') { // the live hops the draft leaves out: removed, with an undo each
    const kept = new Set(draft.hops.map((h) => h.id));
    path.hops.filter((h) => !kept.has(h.id)).forEach((h) => removed.push({ hop: { ...h, joined: false }, index: h.position, live: true, deleteHistory: false }));
  }
  draft.start = JSON.stringify(payload()); // before the automatic links, so a linked-for-you draft counts as changed
  draft.hops.forEach((h) => autoLink(h, matches[h.id]));
  leaveArmed = false;
  stopRunningPreview();
  render();
}

// A home hop without a device: the one device at its address (the server's match, which resolves names; else a
// device whose URL host is the address as written). Marked so the card says it was linked for you.
function autoLink(h, match) {
  if (!DEVICE_ROLES.includes(h.category) || h.device_id || h.device_unlinked) return; // "No device" stays no device
  const known = match && state.devices.some((d) => d.id === match) ? match : null;
  const address = String(h.address || '').toLowerCase().replace(/\.$/, '');
  const at = state.devices.filter((d) => deviceHost(d) === address);
  const id = known || (at.length === 1 ? at[0].id : null);
  if (id) { h.device_id = id; h.autoLinked = true; }
}

// What Path health draws as one node (panels/diagnosis, path.js pathNodes): every hop of your network, every hop of
// the line, and provider hops or public servers merged in the editor (Merge / Split). DNS lookups and other targets
// are no stage of the path: a node each, drawn apart.
const MERGEABLE = ['isp', 'public'];
const ONE_NODE = ['local', 'gateway'];
function chainRuns(hops) {
  const out = [];
  hops.forEach((h, i) => {
    const prev = hops[i - 1];
    const joins = !!prev && prev.category === h.category && (ONE_NODE.includes(h.category) || (MERGEABLE.includes(h.category) && h.joined));
    if (joins) out[out.length - 1].push(h); else out.push([h]);
  });
  return out;
}

// Groups from the merged runs: a run of joined hops of one role shares a group (its first existing group name, else
// "internet" for public servers ("internet_v6" for IPv6 ones, Path health's own node) or one made from the first hop's
// id); a lone hop has none, except a public server beside other runs of them: Path health draws every public server
// without a group in one node, so each run of them gets a group of its own.
function runs(hops) {
  const out = [];
  hops.forEach((h, i) => {
    const joins = i > 0 && h.joined && hops[i - 1].category === h.category;
    if (joins) out[out.length - 1].push(h); else out.push([h]);
  });
  return out;
}

function grouped(hops) {
  const used = new Set();
  const out = [];
  const all = runs(hops);
  const publicRuns = all.filter((r) => r[0].category === 'public').length;
  all.forEach((run) => {
    let group = null;
    if (run.length > 1 || (run[0].category === 'public' && publicRuns > 1)) {
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
    if (!DEVICE_ROLES.includes(hop.category)) hop.device_id = null; // a device link means something on a home hop only
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

const hopById = (id) => draft.hops.find((h) => h.id === id);

// An IP literal is probed as it is (its own family); only a host name has a family to pick.
const isName = (address) => !address.includes(':') && !/^[0-9.]+$/.test(address);
const familyOf = (h) => (h.address.includes(':') ? 6 : isName(h.address) ? (h.family || 4) : 4);

// The live hop the prober measures this draft hop as: the same id at the same address and family, else any live hop at
// that address and family (a draft from discovery or a file may name it otherwise); null for a hop not probed yet.
function liveTwin(h) {
  const same = (o) => o && o.address === h.address && familyOf(o) === familyOf(h) && o.enabled !== false && live.has(o.id);
  const was = liveHops.get(h.id);
  if (same(was)) return was.id;
  const other = [...liveHops.values()].find(same);
  return other ? other.id : null;
}
const probedLive = (h) => liveTwin(h) !== null;

// ---------- the measurement (Path health's figures and words) ----------
const has = (v) => v !== null && v !== undefined;
const fmtMs = (v) => (v >= 100 ? String(Math.round(v)) : (+v).toFixed(1));
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
    const t = live.get(twin);
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
    return { level, html: `<b class="num">${fmtMs(ms)} ms</b> · <span class="num">${fmtLoss(loss)}</span> loss ${LAST_MINUTE}`, ms };
  }
  const p = preview && preview.hops[h.id] && preview.addresses[h.id] === h.address ? preview.hops[h.id] : null;
  if (p) {
    const when = preview.state === 'done' ? `<span class="muted">(preview, ${PREVIEW_S} s)</span>` : `<span class="muted">(previewing, ${Math.round(preview.elapsed || 0)} s)</span>`;
    if (p.kind === 'ipv6_unavailable') return { level: 'unknown', html: `${escapeHtml(NO_IPV6)} ${when}`, title: NO_IPV6_WHY, word: 'Not probed' };
    if (!p.received) {
      if (p.kind === 'unreachable') return { level: 'crit', html: `<b>Unreachable</b> ${when}`, title: 'A router on the way says this address cannot be reached.', word: 'Unreachable' };
      return { level: p.sent ? 'warn' : 'unknown', html: `${p.sent ? 'No reply' : 'Starting'} ${when}`, title: p.sent ? 'No answer yet: it may ignore ping, or be down. Once it is on the path, Pengumon tells the two apart.' : '', word: 'No reply' };
    }
    const level = p.loss_pct > 0 ? 'warn' : 'good';
    return { level, html: `<b class="num">${fmtMs(p.median_ms)} ms</b> · <span class="num">${fmtLoss(p.loss_pct)}</span> loss ${when}`, ms: p.median_ms };
  }
  if (tests.has(h.id)) return { level: 'unknown', html: tests.get(h.id), test: true, word: 'Tested' };
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
  const hops = draft.hops;
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

const chainValue = (m) => (has(m.ms) ? `<b class="num">${fmtMs(m.ms)}</b> ms` : escapeHtml(m.word || ''));

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
  const chooser = picking.has(h.id)
    ? `<label class="field pe-dev-pick"><span>Device</span><select class="input" data-change="pickPathDevice" data-arg="${id}" aria-label="Device of ${escapeHtml(h.name)}">${options(choices, h.device_id || '')}</select></label>` : '';
  let line;
  let why = NO_DEVICE_WHY;
  if (h.device_id) {
    const auto = h.autoLinked ? ' <span class="muted">· linked by its address</span>' : '';
    const now = device ? ` · <span class="pe-dev-now" data-dev-now="${escapeHtml(device.id)}">${deviceNow(device)}</span>` : ' · <span class="muted">not configured on this install</span>';
    line = `${icon(device ? 'plug' : 'warn', 'sm')}<span>Readings from: <b class="pe-dev-name">${escapeHtml(device ? deviceLabel(device) : h.device_id)}</b>${now}${auto}</span>`
      + `<button type="button" class="btn sm" data-action="changePathDevice" data-arg="${id}" aria-expanded="${picking.has(h.id)}">Change</button>`;
    if (device) {
      const panel = deviceStatus(device, null);
      why = `${escapeHtml(DEVICE_WHY)} <button type="button" class="pe-lnk" data-action="showDevicePanel" data-arg="${escapeHtml(panel.panel)}">${escapeHtml(panel.panelName)} panel</button>.`;
    } else {
      why = 'That device is not in this install\'s config.yaml: nothing reads it here.';
    }
  } else if (state.devices.length) {
    line = `${icon('plug', 'sm')}<span class="muted">No device connected</span> · <button type="button" class="btn sm outline" data-action="connectPathDevice" data-needs-edit data-arg="${id}">${icon('lock')}Connect</button>`;
  } else {
    line = `${icon('plug', 'sm')}<span class="muted">No device connected</span> · <span class="fine">add one under <code>devices:</code> in config.yaml to read it</span>`;
  }
  const driver = !h.device_id && h.suggested_driver
    ? `<p class="fine pe-suggest">Discovery thinks this is a device the <code>${escapeHtml(h.suggested_driver)}</code> driver can read${state.devices.some((d) => d.driver === h.suggested_driver) ? '' : `: add it under <code>devices:</code> in config.yaml (driver <code>${escapeHtml(h.suggested_driver)}</code>)`}.</p>` : '';
  return `<div class="pe-dev"><div class="pe-dev-l">${line}</div>${chooser}<p class="fine pe-dev-why">${why}</p>${driver}</div>`;
}

// The readings of the linked devices, followed while the editor is open (the device cards' own feed).
function watchDevices() {
  if (!draft) return;
  const want = new Set(draft.hops.filter((h) => DEVICE_ROLES.includes(h.category) && h.device_id).map((h) => h.device_id));
  state.devices.filter((d) => want.has(d.id) && !deviceWatch.has(d.id)).forEach((d) => {
    deviceWatch.set(d.id, watchDevice(bus, d, () => paintDevice(d)));
  });
}

function paintDevice(device) {
  const html = deviceNow(device);
  document.querySelectorAll(`#pathEditorHops [data-dev-now="${CSS.escape(device.id)}"]`).forEach((el) => {
    if (el.innerHTML !== html) el.innerHTML = html;
  });
}

function unwatchDevices() {
  deviceWatch.forEach((off) => off());
  deviceWatch = new Map();
}

// The Router or Line panel, where the device's details are: the editor closes (a changed draft asks first).
let leaveArmed = false;
function showDevicePanel(el) {
  if (draft && JSON.stringify(payload()) !== draft.start && !leaveArmed) {
    leaveArmed = true;
    note('warn', '<b>Your changes are not applied.</b> Apply them first, or press the link again to leave them and see the panel.');
    return;
  }
  leaveArmed = false;
  const panel = el.dataset.arg === 'line' ? 'line' : 'router';
  closeModal('pathEditorModal');
  const tab = document.querySelector(`[data-action=selectTab][data-arg="${panel === 'line' ? 'line' : 'network'}"]`);
  if (tab && getComputedStyle(tab).display !== 'none' && tab.offsetParent !== null) tab.click();
  setTimeout(() => {
    const target = document.querySelector(`[data-panel="${panel}"] .panel`) || document.querySelector(`[data-panel="${panel}"]`); // the card: its section box may be display: contents
    if (target) target.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }, 50);
}

function addressForm(h) {
  if (addressing !== h.id) return '';
  const id = escapeHtml(h.id);
  return `<form class="pe-addr-f" data-submit="replacePathAddress" data-arg="${id}" novalidate>
    <label class="field grow"><span>New address of ${escapeHtml(h.name)}</span><input class="input mono" type="text" value="${escapeHtml(h.address)}" maxlength="255" autocomplete="off" autocapitalize="off" spellcheck="false" data-f="address" aria-describedby="peAddrWhy-${id}"></label>
    <button type="submit" class="btn sm primary">Replace</button><button type="button" class="btn sm" data-action="cancelPathAddress" data-arg="${id}">Cancel</button>
    <p class="fine" id="peAddrWhy-${id}" data-f="why">${escapeHtml(ADDRESS_WHY)}</p></form>`;
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
    ${deviceBlock(h)}${evidence}</li>`;
}

function renderRemoved() {
  const box = $('pathEditorRemoved');
  box.hidden = !removed.length;
  box.innerHTML = removed.length ? `<h3 class="section-t">Removed when you apply <span class="muted">${removed.length}</span></h3><ul class="pe-gone">${removed.map((r) => {
    const id = escapeHtml(r.hop.id);
    const history = r.live ? `<label class="check"><input type="checkbox" data-change="togglePathHistory" data-arg="${id}"${r.deleteHistory ? ' checked' : ''}> Delete its history too (cannot be undone)</label>` : '';
    return `<li data-removed="${id}"><span class="pe-gone-n"><b>${escapeHtml(r.hop.name)}</b> <span class="mono">${escapeHtml(r.hop.address)}</span></span>${history}<button type="button" class="btn sm" data-action="undoRemovePathHop" data-needs-edit data-arg="${id}">Undo</button></li>`;
  }).join('')}</ul>` : '';
}

// The names with a "Hop N:" prefix (older configs numbered them; Path health numbers the hops itself): one offer.
function renderClean() {
  const dirty = draft.hops.filter((h) => cleanName(h.name));
  $('pathEditorClean').innerHTML = dirty.length ? `<div class="result st-unknown pe-clean">${icon('info')}<span><b>${dirty.length} name${dirty.length === 1 ? ' starts' : 's start'} with "Hop N:"</b> (${escapeHtml(dirty.slice(0, 2).map((h) => `"${h.name}"`).join(', '))}${dirty.length > 2 ? ', …' : ''}). Path health numbers the hops itself.</span>`
    + '<button type="button" class="btn sm outline" data-action="cleanPathNames" data-needs-edit>Drop the prefixes</button></div>' : '';
}

function render(focus) {
  const hops = draft.hops;
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
    import: 'a draft from a file · nothing changes until Apply', version: `revision ${draft.revision} as a draft · nothing changes until Apply`
  };
  $('pathEditorMeta').textContent = meta[draft.source];
  permissions();
  if (focus) { // the control that was used, on the re-drawn card
    const which = focus.dir ? `[data-dir="${focus.dir}"]` : focus.field ? `[data-field="${focus.field}"]` : focus.selector || '[data-field="name"]';
    const el = document.querySelector(`#pathEditorHops [data-arg="${CSS.escape(focus.id)}"]${which}`) || document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(focus.id)}"] ${which}`);
    if (el && !el.disabled) el.focus();
    else (document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(focus.id)}"] .pe-name`) || $('btnApplyPath')).focus();
  }
}

// Without the right to edit (a viewer who has not signed in): look, but no Apply, Rediscover, Preview or Restore.
function permissions() {
  const ok = canEdit();
  $('pathEditorReadOnly').hidden = ok;
  // the tooltip and the blocked click are js/util.js's (data-needs-edit); these are off outright as well
  ['btnApplyPath', 'btnRediscover', 'btnPreviewPath'].forEach((b) => { $(b).disabled = !ok || (b === 'btnApplyPath' && applying); });
  document.querySelectorAll('#pathVersionList [data-action=loadPathVersion]').forEach((b) => { b.disabled = !ok; });
}

// The live measurement of every card and chain node, in place (typing in a name field keeps its focus).
function paintLive() {
  if (!draft) return;
  draft.hops.forEach((h) => {
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

function onTargets(targets) {
  live = new Map((targets || []).map((t) => [t.target_id, t]));
  paintLive();
}

// ---------- opening and closing ----------
async function open(arg = '') {
  note('', '');
  openModal('pathEditorModal');
  if (!unsubscribe) unsubscribe = bus.subscribe('targets', onTargets);
  $('pathEditorChain').innerHTML = '';
  $('pathEditorHops').innerHTML = '<li class="skel-stack" aria-busy="true"><div class="skel line" style="width:90%"></div><div class="skel line" style="width:70%"></div></li>';
  try {
    const path = await fetchPath();
    if (arg.startsWith('discovery:')) {
      const res = await fetch(`/api/discovery/${encodeURIComponent(arg.slice(10))}`);
      if (!res.ok) throw new Error(await errorText(res));
      showDiscovery(path, await res.json());
    } else {
      load(path, path.hops, 'live', { matches: path.device_matches || {} });
    }
  } catch (e) {
    note('crit', `<b>The path could not be loaded.</b> ${escapeHtml(e.message)}`);
  }
  loadVersions();
}

function closed() {
  if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  unwatchDevices();
  stopRunningPreview();
  draft = null;
  removed = [];
}

function showDiscovery(path, run) {
  if (run.state !== 'done' || !run.draft) {
    note('crit', `<b>Discovery did not finish.</b> ${escapeHtml(run.progress || '')} ${(run.notes || []).map(escapeHtml).join(' ')}`);
    if (!draft) load(path, path.hops, 'live', { matches: path.device_matches || {} });
    return;
  }
  load(path, run.draft.hops || [], 'discovery', { notes: run.notes || [], matches: path.device_matches || {} });
  const counts = ['new', 'changed', 'gone'].map((s) => [s, draft.hops.filter((h) => h.status === s).length]).filter(([, c]) => c);
  const diff = counts.length ? ` Compared with the live path: ${counts.map(([s, c]) => `${c} ${s === 'gone' ? 'not found' : s}`).join(', ')}.` : '';
  const notes = (run.notes || []).length ? `<ul class="pe-notes">${run.notes.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>` : '';
  note('unknown', `<b>Discovery found ${draft.hops.length} hop${draft.hops.length === 1 ? '' : 's'}.</b> Check each one's role and reasons, then Apply.${diff}${notes}`);
}

async function rediscover() {
  if (!draft || !canEdit()) return;
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
    button.disabled = !canEdit();
  }
}

// ---------- earlier paths ----------
const when = (s) => new Date(s * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });

async function loadVersions() {
  const box = $('pathVersions');
  let versions;
  try { versions = await fetchVersions(); } catch (_) { versions = null; }
  if (!Array.isArray(versions) || !draft) { box.hidden = true; return; }
  const current = draft.base;
  const older = versions.filter((v) => v.revision !== current);
  box.hidden = !older.length;
  if (!older.length) return;
  const previous = older[0];
  const row = (v) => `<li><span class="pe-ver-t"><b>Revision ${escapeHtml(String(v.revision))}</b> <span class="muted">${escapeHtml(when(v.applied_at))} · ${escapeHtml(String(v.hop_count))} hop${v.hop_count === 1 ? '' : 's'}</span><span class="pe-ver-s">${escapeHtml(v.summary || '')}</span></span>`
    + `<button type="button" class="btn sm" data-action="loadPathVersion" data-needs-edit data-arg="${escapeHtml(String(v.revision))}">Load</button></li>`;
  $('pathVersionList').innerHTML = `<button type="button" class="btn sm outline" data-action="loadPathVersion" data-needs-edit data-arg="${escapeHtml(String(previous.revision))}">${icon('history')}Restore the previous path</button>`
    + `<ul class="pe-gone pe-vers">${older.slice(0, 50).map(row).join('')}</ul>`;
  permissions();
}

async function loadVersion(el) {
  if (!canEdit()) return;
  try {
    const [path, version] = await Promise.all([fetchPath(), fetchVersion(el.dataset.arg)]);
    load(path, (version.hops || []).map((h, i) => ({ position: i, ...h })), 'version', { revision: version.revision, matches: path.device_matches || {} });
    note('unknown', `<b>Revision ${escapeHtml(String(version.revision))} is in the editor as a draft.</b> Review it, then Apply to restore it; a hop that comes back keeps its history.`);
    $('pathEditorChain').scrollIntoView({ block: 'nearest' });
  } catch (e) {
    note('crit', `<b>That path could not be loaded.</b> ${escapeHtml(e.message)}`);
  }
}

// ---------- preview ----------
function stopRunningPreview() {
  if (preview && preview.state === 'running') stopPreview(preview.id);
  if (preview) preview.state = 'stopped';
  preview = null;
  const b = $('btnPreviewPath');
  if (b) b.querySelector('span').textContent = 'Preview';
}

async function runPreview() {
  if (!draft || !canEdit()) return;
  if (preview && preview.state === 'running') { stopRunningPreview(); paintLive(); return; } // a second press stops it
  const unprobed = draft.hops.filter((h) => !probedLive(h) && h.enabled !== false);
  if (!unprobed.length) { note('unknown', 'Every hop of the draft is probed already: its live figures are on its card.'); return; }
  const label = $('btnPreviewPath').querySelector('span');
  try {
    if (preview) stopRunningPreview(); // a finished one: its figures give way to the new run's
    const { id, probed } = await startPreview(payload());
    preview = { id, state: 'running', elapsed: 0, hops: {}, addresses: Object.fromEntries(draft.hops.map((h) => [h.id, h.address])) };
    label.textContent = 'Stop preview';
    const n = (probed || unprobed).length;
    note('unknown', `<b>Previewing ${n} hop${n === 1 ? '' : 's'}</b> for ${PREVIEW_S} s: one probe a second, nothing recorded. Their figures fill in on the cards.`);
    paintLive();
    const mine = preview;
    while (mine.state === 'running') {
      await new Promise((r) => setTimeout(r, 1000));
      if (mine.state !== 'running') break;
      const r = await readPreview(id);
      Object.assign(mine, { hops: r.hops || {}, elapsed: r.elapsed_s || 0 });
      if (r.state === 'done') mine.state = 'done';
      paintLive();
    }
    if (mine.state === 'done') {
      label.textContent = 'Preview';
      note('good', `<b>Preview done.</b> ${PREVIEW_S} s of probes are on the cards of the hops that are not probed yet. Apply to keep the path.`);
    }
  } catch (e) {
    if (preview) preview.state = 'failed';
    label.textContent = 'Preview';
    note(e.unavailable ? 'warn' : 'crit', `<b>No preview.</b> ${escapeHtml(e.message)}`);
  }
}

// ---------- edits ----------
function edit(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  const field = el.dataset.field;
  if (field === 'name') { // no re-draw: typing (and Tab) keep the focus; only the "Where" list and the chain name the hops
    h.name = el.value;
    const opt = $('pathAddPosition').querySelector(`option[value="${draft.hops.indexOf(h)}"]`);
    if (opt) opt.textContent = `Before hop ${draft.hops.indexOf(h) + 1}: ${h.name}`;
    document.querySelectorAll(`#pathEditorChain [data-arg="${CSS.escape(h.id)}"] .pe-node-n`).forEach((n) => { n.textContent = h.name; });
    renderClean();
    return;
  }
  if (field === 'category') {
    h.category = el.value;
    autoLink(h);
  } else if (field === 'family') { h.family = Number(el.value) === 6 ? 6 : 4; tests.delete(h.id); }
  render({ id: h.id, field });
}

function move(el) {
  const i = draft.hops.findIndex((h) => h.id === el.dataset.arg);
  const j = i + Number(el.dataset.dir);
  if (i < 0 || j < 0 || j >= draft.hops.length) return;
  const [h] = draft.hops.splice(i, 1);
  h.joined = false;
  if (draft.hops[i]) draft.hops[i].joined = false; // the hop that took its place no longer follows it
  draft.hops.splice(j, 0, h);
  render({ id: h.id, dir: el.dataset.dir });
}

function merge(el, joined) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  h.joined = joined;
  render();
  const again = document.querySelector(`#pathEditorChain [data-arg="${CSS.escape(h.id)}"][data-action="${joined ? 'splitPathHops' : 'mergePathHops'}"]`);
  (again || $('pathEditorChain').querySelector('button') || $('btnApplyPath')).focus();
}

function focusHop(el) {
  const card = document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(el.dataset.arg)}"]`);
  if (!card) return;
  card.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  card.classList.remove('flash');
  void card.offsetWidth; // restarts the highlight
  card.classList.add('flash');
  card.querySelector('.pe-name').focus({ preventScroll: true });
}

function remove(el) {
  const i = draft.hops.findIndex((h) => h.id === el.dataset.arg);
  if (i < 0) return;
  const [h] = draft.hops.splice(i, 1);
  if (draft.hops[i]) draft.hops[i].joined = draft.hops[i].joined && i > 0;
  removed.push({ hop: h, index: i, live: liveHops.has(h.id), deleteHistory: false });
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
    group: null, device_id: null, joined: false, added: true };
  autoLink(hop);
  draft.hops.splice(at, 0, hop);
  $('pathAddAddress').value = '';
  hint.textContent = 'Added to the draft. Test or preview it, then Apply.';
  render({ id: hop.id });
}

function startAddress(el) {
  addressing = el.dataset.arg;
  render();
  const input = document.querySelector(`#pathEditorHops [data-hop="${CSS.escape(addressing)}"] .pe-addr-f [data-f=address]`);
  if (input) { input.focus(); input.select(); }
}

// Replaces a hop's address in place: same id, so the history stays with it (a renumbered provider router).
function replaceAddress(form, ev) {
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
    const was = liveHops.get(h.id);
    h.addressFrom = was && was.address !== address ? was.address : null;
    h.address = address;
    tests.delete(h.id);
  }
  addressing = null;
  render({ id: h.id, selector: '[data-action=editPathAddress]' });
}

function cancelAddress(id) {
  addressing = null;
  render({ id, selector: '[data-action=editPathAddress]' });
}

async function test(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  el.disabled = true;
  tests.set(h.id, 'Testing…');
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
  tests.set(h.id, html);
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
function connect(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  const byDriver = h.suggested_driver ? state.devices.filter((d) => d.driver === h.suggested_driver) : [];
  const single = byDriver.length === 1 ? byDriver[0] : state.devices.length === 1 ? state.devices[0] : null;
  if (!single) {
    picking.add(h.id);
    render({ id: h.id, selector: '[data-change=pickPathDevice]' });
    return;
  }
  h.device_id = single.id;
  h.autoLinked = false;
  h.device_unlinked = false;
  render({ id: h.id, selector: '[data-action=changePathDevice]' });
  openSetup(single.id);
}

function pickDevice(el) {
  const h = hopById(el.dataset.arg);
  if (!h) return;
  h.device_id = el.value || null;
  h.autoLinked = false;
  h.device_unlinked = !h.device_id; // "No device" is a choice: no automatic link undoes it
  picking.delete(h.id);
  render({ id: h.id, selector: h.device_id ? '[data-action=changePathDevice]' : '[data-action=connectPathDevice]' });
  const device = state.devices.find((d) => d.id === h.device_id);
  if (device && !device.has_credentials) openSetup(device.id); // not signed in yet: where to do it
}

function toggleChooser(el) {
  const id = el.dataset.arg;
  if (picking.has(id)) picking.delete(id); else picking.add(id);
  render({ id, selector: picking.has(id) ? '[data-change=pickPathDevice]' : '[data-action=changePathDevice]' });
}

function cleanNames() {
  let n = 0;
  draft.hops.forEach((h) => { const c = cleanName(h.name); if (c) { h.name = c; n++; } });
  render();
  note('unknown', `<b>Dropped the "Hop N:" prefix from ${n} name${n === 1 ? '' : 's'}.</b> Apply to keep the new names.`);
}

async function apply() {
  if (!draft || applying || !canEdit()) return;
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
    stopRunningPreview();
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
    button.disabled = !canEdit();
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
    const { hops, device_matches: matches } = await res.json();
    const path = await fetchPath();
    load(path, hops, 'import', { matches: matches || {} });
    note('unknown', `<b>Read ${hops.length} hop${hops.length === 1 ? '' : 's'} from ${escapeHtml(file.name)}.</b> Review them, then Apply.`);
  } catch (e) {
    note('crit', `<b>The file was not imported.</b> ${escapeHtml(e.message)}`);
  }
}

function cancel() {
  closeModal('pathEditorModal'); // the observer below lets go of the live data and stops a preview
}

registerActions('click', {
  openPathEditor: (el) => open(el.dataset.arg || ''),
  cancelPathEditor: () => cancel(),
  reloadPathEditor: () => open(''),
  movePathHop: (el) => move(el),
  mergePathHops: (el) => merge(el, true),
  splitPathHops: (el) => merge(el, false),
  focusPathHop: (el) => focusHop(el),
  removePathHop: (el) => remove(el),
  undoRemovePathHop: (el) => undo(el),
  testPathHop: (el) => test(el),
  connectPathDevice: (el) => connect(el),
  changePathDevice: (el) => toggleChooser(el),
  editPathAddress: (el) => startAddress(el),
  cancelPathAddress: (el) => cancelAddress(el.dataset.arg),
  cleanPathNames: () => cleanNames(),
  showDevicePanel: (el) => showDevicePanel(el),
  loadPathVersion: (el) => loadVersion(el),
  previewPath: () => runPreview(),
  applyPath: () => apply(),
  rediscoverPath: () => rediscover(),
  importPath: () => $('pathImportFile').click()
});
registerActions('input', { editPathHop: (el, ev) => edit(el, ev) });
registerActions('change', {
  editPathHop: (el, ev) => edit(el, ev),
  pickPathDevice: (el) => pickDevice(el),
  pathAddRoleChanged: (el) => { $('pathAddRoleFx').textContent = ROLE_EFFECT[el.value] || ''; },
  togglePathHistory: (el) => { const r = removed.find((x) => x.hop.id === el.dataset.arg); if (r) r.deleteHistory = el.checked; },
  importPathFile: (el) => importFile(el)
});
registerActions('submit', {
  addPathHop: (el, ev) => addHop(ev),
  replacePathAddress: (el, ev) => replaceAddress(el, ev)
});

// Keyboard: focus moves into the dialog when it opens, Tab stays inside it and Escape closes it. Closing, by any way,
// lets go of the live data and stops a running preview.
function keepKeyboard(modal, firstFocus) {
  let wasOpen = false;
  new MutationObserver(() => {
    const isOpen = modal.classList.contains('active');
    if (isOpen && !modal.contains(document.activeElement)) (modal.querySelector(firstFocus) || modal.querySelector('.m-close')).focus();
    if (wasOpen && !isOpen) closed();
    wasOpen = isOpen;
  }).observe(modal, { attributes: true, attributeFilter: ['class'] });
  modal.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !ev.defaultPrevented) {
      if (addressing) { cancelAddress(addressing); ev.preventDefault(); return; }
      closeModal(modal.id);
      return;
    }
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
