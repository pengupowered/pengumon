// Topology at the top of the Local network modal: Internet - line / modem - router - mesh nodes, each mesh node with its
// client count. An SVG picture drawn by hand (no graph library; a vertical HTML list on narrow screens) that shows only
// what the configured devices and features provide:
//   no line device: no modem node;  no `mesh` feature: router only;  no `clients` feature: no client counts.
// The "Local network" button (on the router card) is offered only when there is something beyond "Internet - router"
// (a line device, or a device that lists clients); the picture is drawn only while the modal is open.
//
// Data: the nodes and the client totals are the /api/devices/{id}/clients answer that clients.js loads while the modal
// is open (setTopologyClients); nothing rides on the websocket. A mesh node (or the lone client
// pill of a router without mesh) filters the client table below.
// The status colour and latency of each hop come from the live message's targets, by category:
//   * local -> router, gateway -> line / modem: the WORST enabled target of the category sets the status and supplies
//     the latency shown. A hop is only as healthy as its weakest probe, and a category normally has one target, so
//     "worst" equals "first" there; with several it is the one a person needs to look at.
//   * Internet: the public targets are redundant probes of the same thing, so one failing is "degraded", and it is
//     "down" only when all of them are; the latency shown is the fastest answer. With no public target the isp, then
//     the dns targets stand in.
// Labels come from the device config (model, else host, else id) and the APIs, nothing else. The structure is built
// only when the set of nodes changes; every message then just updates classes and texts in place.

import { state } from '../../js/state.js';
import { backhaulText, latestMs } from '../../js/util.js';
import { featureAvailable, primaryDevice } from '../../js/devices.js';

// The devices the picture names: the primary line and router (the server's choice, devices.js primaryDevice) and the
// device that lists the clients (the first of the `clients` feature's providers).
const lineDevice = () => primaryDevice('access_line');
const routerDevice = () => primaryDevice('wan_counters') || primaryDevice('router_system');
export function clientsDevice() {
  const f = state.features.find((x) => x.feature === 'clients');
  return f && f.available ? state.devices.find((d) => d.enabled && (f.provided_by || []).includes(d.id)) || null : null;
}

const RANK = { up: 0, degraded: 1, down: 2 };
const CLASS = { up: 'healthy', degraded: 'degraded', down: 'down' };

const view = {
  clients: null, // the last /clients answer
  filter: null, // the mesh node id the client table is filtered to (shown as pressed)
  live: null, // { targets, router }: the last targets and the primary router's last snapshot
  layoutKey: null,
  refs: {}
};

const isOpen = () => !!document.getElementById('networkModal')?.classList.contains('active');

function make(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = text;
  return el;
}

function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function setClass(el, cls) {
  if (el && el.getAttribute('class') !== cls) el.setAttribute('class', cls); // SVG elements have no writable className
}

const pretty = (s) => String(s).replace(/_/g, ' ');
const deviceLabel = (d) => (d.model ? pretty(d.model) : (d.host || d.id));
const ms = (v) => (v === null || v === undefined ? null : `${v} ms`);

// The enabled targets of a category, as the live message lists them.
function inCategory(targets, ...categories) {
  return targets.filter(t => t.enabled !== false && categories.includes(t.category));
}

// A hop's picture: { status, latency } of the worst target of the category, null when it has no target.
export function hopOf(targets, category) {
  const list = inCategory(targets, category);
  if (!list.length) return null;
  const worst = list.reduce((a, b) => (RANK[b.status] > RANK[a.status] ? b : a));
  return { status: worst.status, latency: latestMs(worst) };
}

// The Internet node: the public targets, standing in with isp, then dns ones when there are none.
export function internetOf(targets) {
  const list = [['public'], ['isp'], ['dns']].map(c => inCategory(targets, ...c)).find(l => l.length);
  if (!list) return null;
  const up = list.filter(t => t.status !== 'down');
  const status = !up.length ? 'down' : (list.some(t => t.status !== 'up') ? 'degraded' : 'up');
  const answers = list.map(latestMs).filter(v => v !== null && v !== undefined);
  return { status, latency: answers.length ? Math.min(...answers) : null };
}

export function wanText(router) {
  const rx = router ? router.wan_rx_rate_mbps : null;
  if (rx === null || rx === undefined) return null;
  const f = (v) => (Math.round(v * 10) / 10);
  const tx = router.wan_tx_rate_mbps;
  return `↓${f(rx)}${tx === null || tx === undefined ? '' : ` ↑${f(tx)}`} Mbps`;
}

function meshNodes() {
  return featureAvailable('mesh') && view.clients ? view.clients.nodes : [];
}

function clientTotals() {
  const s = view.clients && clientsDevice() ? view.clients.summary : null;
  return s ? { online: s.online, wired: s.wired, wifi: Math.max(0, s.online - s.wired) } : null;
}

const SVG = 'http://www.w3.org/2000/svg';
const NW = 156; // node box
const NH = 62;
const GAP = 46;
const PILL_W = 128;
const ST = { up: 'good', degraded: 'warn', down: 'crit' };
const STATUS_WORD = { warn: 'Degraded', crit: 'Down' };

function svg(tag, attrs, text) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  if (text !== undefined) el.textContent = text;
  return el;
}

const clip = (text, n) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

function iconUse(name, attrs) {
  const el = svg('svg', { ...attrs, class: 'i', 'aria-hidden': 'true' });
  el.appendChild(svg('use', { href: `#i-${name}` }));
  return el;
}

// One node box of the wide picture: { g, sub } (the sub line is painted in place).
function box(x, y, spec) {
  const g = svg('g', { class: 'tn st-unknown', transform: `translate(${x},${y})` });
  g.appendChild(svg('rect', { width: NW, height: NH, rx: 10 }));
  g.appendChild(svg('rect', { class: 'bar', width: 4, height: NH - 16, y: 8, rx: 2 }));
  g.appendChild(iconUse(spec.ic, { x: 12, y: 12, width: 20, height: 20 }));
  g.appendChild(svg('text', { x: 40, y: 22, class: 'tk' }, spec.kind.toUpperCase()));
  g.appendChild(svg('text', { x: 40, y: 39, class: 'tname' }, clip(spec.name, 17)));
  const sub = g.appendChild(svg('text', { x: 40, y: 54, class: 'tsub' }));
  g.appendChild(svg('title', {}, `${spec.kind}: ${spec.name}`)); // last: the CSS addresses the box as rect:first-child
  return { g, sub };
}

function pill(x, y) {
  const g = svg('g', { class: 'tc', transform: `translate(${x},${y})` });
  g.appendChild(svg('rect', { width: PILL_W, height: 36, rx: 18 }));
  g.appendChild(iconUse('users', { x: 10, y: 8, width: 20, height: 20 }));
  return { g, label: g.appendChild(svg('text', { x: 36, y: 23, class: 'tname' })) };
}

// A group that acts as one button (a mesh node with its client pill, or the lone pill): the delegated data-action
// handler runs on click, and Enter and Space do the same (see initTopology).
function control(g, arg, title) {
  g.classList.add('tgo');
  g.setAttribute('role', 'button');
  g.setAttribute('tabindex', '0');
  g.setAttribute('aria-pressed', 'false');
  g.dataset.action = 'setClientsNodeFilter';
  g.dataset.arg = arg;
  g.setAttribute('aria-label', title);
  return g;
}

// What the card has to draw, structurally: changes only when a device, a feature or a mesh node does.
function layoutKey(targets) {
  const nodes = meshNodes();
  return JSON.stringify([
    !!internetOf(targets), lineDevice() ? deviceLabel(lineDevice()) : null,
    routerDevice() ? deviceLabel(routerDevice()) : null, !!hopOf(targets, 'local'), !!clientsDevice(), featureAvailable('line_load'),
    nodes.map(n => [n.id, n.label]), !!clientTotals()
  ]);
}

function build(targets) {
  const body = document.getElementById('topologyBody');
  body.replaceChildren();
  const refs = { cols: [], links: [], leaves: new Map(), pills: [] };
  const specs = [];
  if (internetOf(targets)) specs.push({ id: 'internet', ic: 'globe', kind: 'Internet', name: 'Public targets' });
  if (lineDevice()) specs.push({ id: 'line', ic: 'modem', kind: 'Line', name: deviceLabel(lineDevice()) });
  if (routerDevice() || hopOf(targets, 'local')) {
    specs.push({ id: 'router', ic: 'router', kind: 'Router', name: routerDevice() ? deviceLabel(routerDevice()) : 'Local router' });
  }
  refs.hasRouter = specs.some(sp => sp.id === 'router');
  const nodes = refs.hasRouter ? meshNodes() : [];
  const listing = !!clientsDevice();
  const rows = Math.max(1, nodes.length);
  const H = Math.max(NH + 20, rows * (NH + 16) + 4);
  const midY = H / 2 - NH / 2;
  const meshX = specs.length * (NW + GAP);
  const routerRight = (specs.length - 1) * (NW + GAP) + NW;
  const g = svg('g');
  specs.forEach((sp, i) => {
    const x = i * (NW + GAP);
    const n = box(x, midY, sp);
    n.id = sp.id;
    n.spec = sp;
    refs.cols.push(n);
    g.appendChild(n.g);
    if (i < specs.length - 1) {
      const link = svg('line', { class: 'tl st-unknown', x1: x + NW, x2: x + NW + GAP, y1: midY + NH / 2, y2: midY + NH / 2 });
      refs.links.push({ el: link, to: refs.cols[i + 1] || null, next: i + 1 });
      g.insertBefore(link, g.firstChild);
    }
  });
  let width = routerRight + 4;
  if (nodes.length) {
    nodes.forEach((n, i) => {
      const y = i * (NH + 16) + 2;
      const path = svg('path', { class: 'tl st-unknown', d: `M${routerRight} ${midY + NH / 2}H${routerRight + GAP / 2}V${y + NH / 2}H${meshX}`, fill: 'none' });
      g.insertBefore(path, g.firstChild);
      const leaf = box(meshX, y, { ic: 'mesh', kind: 'Mesh node', name: n.label });
      let wrap = leaf.g;
      let pl = null;
      if (listing) {
        wrap = control(svg('g'), n.id, `Show the clients on ${n.label}`);
        pl = pill(meshX + NW + 14, y + 13);
        wrap.append(leaf.g, pl.g);
      }
      g.appendChild(wrap);
      refs.leaves.set(n.id, { ...leaf, pill: pl, wrap });
    });
    width = meshX + NW + 14 + (listing ? PILL_W : 0) + 4;
  } else if (listing && refs.hasRouter) {
    const wrap = control(svg('g'), '', 'Show every client');
    const pl = pill(routerRight + 14, midY + 13);
    wrap.appendChild(pl.g);
    g.appendChild(wrap);
    refs.lone = { pill: pl, wrap };
    width = routerRight + 14 + PILL_W + 4;
  }
  const root = svg('svg', { class: 'topo', viewBox: `-2 -2 ${width + 4} ${H + 4}`, role: 'group', 'aria-label': 'Network topology' });
  root.style.maxWidth = `${width + 4}px`;
  root.appendChild(g);
  const wide = make('div', 'topo-wide');
  wide.appendChild(root);
  // Narrow (a phone): the same picture as a vertical tree in HTML.
  const list = make('ol', 'topo-narrow');
  const li = (ic, kind, name, depth) => {
    const el = list.appendChild(make('li', `tv st-unknown d${depth}`));
    el.appendChild(make('span', 'tv-ic')).appendChild(iconUse(ic, {}));
    const text = el.appendChild(make('span'));
    text.appendChild(make('span', 'tv-k', kind));
    text.appendChild(make('b', null, name));
    const sub = text.appendChild(make('span', 'tv-s'));
    const state = el.appendChild(make('span', 'tv-st'));
    return { el, sub, state };
  };
  refs.cols.forEach(n => { n.li = li(n.spec.ic, n.spec.kind, n.spec.name, 0); });
  if (nodes.length) {
    nodes.forEach(n => {
      const leaf = refs.leaves.get(n.id);
      leaf.li = li('mesh', 'Mesh node', n.label, 1);
      if (listing) {
        control(leaf.li.el, n.id, `Show the clients on ${n.label}`);
      }
    });
  } else if (listing && refs.hasRouter) {
    refs.lone.li = li('users', 'Clients', '', 1);
    control(refs.lone.li.el, '', 'Show every client');
  }
  body.append(wide, list);
  view.refs = refs;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function paintStatus(n, hop) {
  const st = hop ? ST[hop.status] : 'unknown';
  setClass(n.g, `tn st-${st}`);
  setClass(n.li.el, `tv st-${st} d0`);
  setText(n.li.state, STATUS_WORD[st] || '');
  return st;
}

function paint() {
  const { targets, router } = view.live || { targets: [], router: null };
  const r = view.refs;
  if (!r.cols) return;
  const hops = { internet: internetOf(targets), line: hopOf(targets, 'gateway'), router: hopOf(targets, 'local') };
  const latency = (h) => (h ? ms(h.latency) || 'no answer' : '');
  const wan = featureAvailable('line_load') ? wanText(router) : null;
  r.cols.forEach(n => {
    paintStatus(n, hops[n.id]);
    let narrow = latency(hops[n.id]);
    let wide = narrow;
    if (n.id === 'router' && wan) {
      wide = wan;
      narrow = [narrow, wan].filter(Boolean).join(' · ');
    }
    setText(n.sub, wide);
    setText(n.li.sub, narrow);
  });
  r.links.forEach(l => {
    const cls = [...(r.cols[l.next] ? r.cols[l.next].g.classList : [])].find(c => c.startsWith('st-')) || 'st-unknown';
    setClass(l.el, `tl ${cls}`);
  });
  const byId = view.clients ? view.clients.summary.by_node : {};
  const t = clientTotals();
  meshNodes().forEach(n => {
    const leaf = r.leaves.get(n.id);
    if (!leaf) return;
    const count = byId[n.id] !== undefined ? byId[n.id] : n.clients;
    const wired = n.wired || 0;
    const wifi = Math.max(0, count - wired);
    const detail = [wifi || !wired ? `${wifi} Wi-Fi` : null, wired ? `${wired} wired` : null].filter(Boolean).join(' · ');
    const off = n.online === false;
    const link = backhaulText(n);
    setText(leaf.sub, off ? 'Offline' : detail);
    setText(leaf.li.sub, [clientsDevice() ? `${plural(count, 'client')}${detail ? ` · ${detail}` : ''}` : '', link].filter(Boolean).join(' · '));
    setClass(leaf.g, `tn st-${off ? 'warn' : 'unknown'}`);
    setClass(leaf.li.el, `tv st-${off ? 'warn' : 'unknown'} d1${leaf.li.el.classList.contains('tgo') ? ' tgo' : ''}`);
    setText(leaf.li.state, off ? 'Offline' : '');
    leaf.g.querySelector('title').textContent = `Mesh node: ${n.label}${off ? ' (offline)' : ''}${link ? `, backhaul ${link}` : ''}`;
    if (leaf.pill) setText(leaf.pill.label, plural(count, 'client'));
  });
  if (r.lone && t) {
    setText(r.lone.pill.label, plural(t.online, 'client'));
    setText(r.lone.li.el.querySelector('b'), `${t.online} online`);
    setText(r.lone.li.sub, `${t.wired} wired · ${t.wifi} Wi-Fi`);
  }
  // The selected filter, on the groups that set it.
  const sel = view.filter || '';
  r.leaves.forEach((leaf, id) => {
    for (const el of [leaf.wrap, leaf.li.el]) if (el.hasAttribute('aria-pressed')) el.setAttribute('aria-pressed', String(sel === id));
  });
  const summary = document.getElementById('topologySummary');
  if (summary) {
    const nodes = meshNodes().length;
    setText(summary, [nodes ? plural(nodes, 'mesh node') : 'single router', t ? plural(t.online, 'client') : null].filter(Boolean).join(' · '));
  }
}

// Whether there is anything beyond "Internet - router" to show: decided by the configured devices and features, so the
// button is right before any client data has been loaded.
export function topologyOffered() {
  return !!lineDevice() || !!clientsDevice();
}

// Redraws the picture for the current devices, features, client data and live message (only while the modal is open).
function render() {
  if (!isOpen()) return;
  const body = document.getElementById('topologyBody');
  const targets = view.live ? view.live.targets : [];
  if (!topologyOffered()) {
    body.replaceChildren();
    view.layoutKey = null;
    return;
  }
  const key = layoutKey(targets);
  if (key !== view.layoutKey) {
    view.layoutKey = key;
    build(targets);
  }
  paint();
}

function update(targets) {
  const router = routerDevice();
  view.live = { targets, router: router && bus ? bus.last(`device:${router.id}`) : null };
  render();
}

// Called by clients.js with every /clients answer it loads, and with null when the modal opens without one.
export function setTopologyClients(data) {
  view.clients = data;
  render();
}

// Called by clients.js when the node filter changes, to mark the pressed node.
export function setTopologyFilter(nodeId) {
  view.filter = nodeId || null;
  if (view.refs.cols) paint();
}

// Follows the live targets (only draws while the modal is open). Idempotent.
let bus = null;
export function initTopology(ctx) {
  if (bus) return;
  bus = ctx.bus;
  bus.subscribe('targets', update);
  // Enter and Space press a topology control, as on a button.
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const el = ev.target instanceof Element ? ev.target.closest('#topologyBody [role=button][data-action]') : null;
    if (!el) return;
    ev.preventDefault();
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}
