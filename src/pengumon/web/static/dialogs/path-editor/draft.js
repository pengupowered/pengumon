// The path editor's draft: the hops being edited, the state around them, and the rules that turn them into what is
// applied (runs, groups, the payload). Draws nothing but the note line.

import { DEVICE_ROLES, deviceHost } from '../../js/path-api.js';
import { state } from '../../js/state.js';
import { icon, slug } from '../../js/util.js';

export const $ = (id) => document.getElementById(id);
const UI_FIELDS = ['joined', 'added', 'evidence', 'confidence', 'suggested_driver', 'status', 'addressFrom', 'autoLinked', 'moreOpen'];

// The editor's state, one object so that every module of this folder shares it (a module cannot assign another's `let`).
export const pe = {
  draft: null, // {base, hops, source: 'live' | 'discovery' | 'import' | 'version', notes, revision}
  removed: [], // [{hop, index, live, deleteHistory}]
  liveHops: new Map(), // id -> the hop as it is live (GET /api/path), to tell a hop probed now from a new or changed one
  tests: new Map(), // hop id -> its last test result, as markup
  picking: new Set(), // hop ids whose device chooser is open
  addressing: null, // the hop id whose address is being replaced
  live: new Map(), // target id -> the live topic's target (prober statistics)
  preview: null, // {id, state, elapsed, hops: {id: result}, addresses: {id: address}}
  unsubscribe: null,
  deviceWatch: new Map(), // device id -> unsubscribe from its readings (js/device-feed.js), while the editor is open
  applying: false,
  leaveArmed: false,
  backGuard: false,
  opener: null, // what had the focus when the editor opened: it gets it back on closing
  pendingReplace: null // the load that waits for "Replace the draft" (confirmReplaceDraft)
};

export function note(level, html) {
  $('pathEditorNote').innerHTML = html ? `<div class="result st-${level}">${icon(level === 'good' ? 'ok' : level === 'crit' ? 'down' : level === 'warn' ? 'warn' : 'unknown')}<span>${html}</span></div>` : '';
}

// ---------- the draft ----------
// Makes `hops` (of `source`) the draft; the caller draws it. `matches`: the server's device matches (GET /api/path).
export function startDraft(path, hops, source, { notes = [], matches = {}, revision = null } = {}) {
  pe.draft = { base: path.revision, source, notes, revision, hops: hops.slice().sort((a, b) => a.position - b.position).map((h) => ({ ...h })) };
  pe.draft.hops.forEach((h, i) => {
    delete h.ignores_ping; // observed, never set (an older export or draft may still carry it)
    const prev = pe.draft.hops[i - 1];
    // public servers without a group are one node on Path health ("internet"), as are those of one group
    h.joined = !!prev && (h.category === 'public' && prev.category === 'public'
      ? (h.group || 'internet') === (prev.group || 'internet') : !!h.group && h.group === prev.group);
  });
  pe.liveHops = new Map(path.hops.map((h) => [h.id, h]));
  pe.removed = [];
  pe.tests = new Map();
  pe.picking = new Set();
  pe.addressing = null;
  if (source === 'import' || source === 'version') { // the live hops the draft leaves out: removed, with an undo each
    const kept = new Set(pe.draft.hops.map((h) => h.id));
    path.hops.filter((h) => !kept.has(h.id)).forEach((h) => pe.removed.push({ hop: { ...h, joined: false }, index: h.position, live: true, deleteHistory: false }));
  }
  pe.draft.start = JSON.stringify(payload()); // before the automatic links, so a linked-for-you draft counts as changed
  pe.draft.hops.forEach((h) => autoLink(h, matches[h.id]));
  // What Cancel compares with: the automatic links are not the owner's edits. A draft that is not the live path
  // (Rediscover, a version, a file) is not what is applied, so it always counts as changed (null).
  pe.draft.opened = source === 'live' ? JSON.stringify(payload()) : null;
  pe.draft.loaded = JSON.stringify(payload()); // what the draft was when loaded: edits are the difference from it
}

// Whether the owner edited the draft since it was loaded (an unsaved draft that is replaced asks first).
export function edited() {
  return !!pe.draft && JSON.stringify(payload()) !== pe.draft.loaded;
}

// A home hop without a device: the one device at its address (the server's match, which resolves names; else a
// device whose URL host is the address as written). Marked so the card says it was linked for you.
export function autoLink(h, match) {
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
export const MERGEABLE = ['isp', 'public'];
export const ONE_NODE = ['local', 'gateway'];
export function chainRuns(hops) {
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

export function payload() {
  return grouped(pe.draft.hops).map((h, i) => {
    const hop = { ...h, position: i };
    UI_FIELDS.forEach((f) => delete hop[f]);
    if (!DEVICE_ROLES.includes(hop.category)) hop.device_id = null; // a device link means something on a home hop only
    return hop;
  });
}

export function freshId(category, address) {
  const taken = new Set([...pe.draft.hops, ...pe.removed.map((r) => r.hop)].map((h) => h.id));
  const base = `${category}_${slug(address) || 'hop'}`.slice(0, 56);
  let id = base;
  for (let k = 2; taken.has(id); k++) id = `${base}_${k}`;
  return id;
}

export const hopById = (id) => pe.draft.hops.find((h) => h.id === id);

// An IP literal is probed as it is (its own family); only a host name has a family to pick.
export const isName = (address) => !address.includes(':') && !/^[0-9.]+$/.test(address);
export const familyOf = (h) => (h.address.includes(':') ? 6 : isName(h.address) ? (h.family || 4) : 4);

// The live hop the prober measures this draft hop as: the same id at the same address and family, else any live hop at
// that address and family (a draft from discovery or a file may name it otherwise); null for a hop not probed yet.
export function liveTwin(h) {
  const same = (o) => o && o.address === h.address && familyOf(o) === familyOf(h) && o.enabled !== false && pe.live.has(o.id);
  const was = pe.liveHops.get(h.id);
  if (same(was)) return was.id;
  const other = [...pe.liveHops.values()].find(same);
  return other ? other.id : null;
}
export const probedLive = (h) => liveTwin(h) !== null;

// Whether the draft differs from what the editor opened with (a device linked for you does not count).
export function changed() {
  return !!pe.draft && (pe.draft.opened === null || JSON.stringify(payload()) !== pe.draft.opened);
}
