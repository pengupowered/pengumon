// Router: one card per enabled device offering wan_counters or router_system (two routers, two cards). Each part only
// when the device offers what it shows: WAN traffic against the line capacity (wan_counters), CPU and memory
// (router_system), the clients by band and the mesh nodes (when the device is a provider of the `clients` / `mesh`
// features). Colors are the server's grades; the capacity and the load are the server's too (the live `line` part and
// the snapshot's wan_*_load_pct).
//
// This is the card's model: it watches the device, fetches the last hour of WAN traffic, keeps the frame (title, badge,
// read time, footer buttons) and makes the view model every view of the card draws (view.js is the standard one;
// docs/themes.md, "Drawing a card"). The view model has one part per capability, null when the device lacks it:
//   { error, dim, notice, wan, system, clients, mesh }

import { backhaulText, cloneTemplate, deviceName, setText } from '../../js/util.js';
import { createCard } from '../../js/cards.js';
import { watchDevice } from '../../js/device-feed.js';
import { errorOf, paintCommon, partPusher, placeCard } from '../../js/device-panel.js';
import { gradeOf, levelOfGrade } from '../../js/grades.js';
import { clientsDevice, topologyOffered } from './topology.js';
import standard from './view.js';

const has = (v) => v !== null && v !== undefined;
const REFRESH_MS = 60000;
const providers = (ctx, feature) => {
  const f = ctx.features.find((x) => x.feature === feature);
  return f && f.available ? f.provided_by || [] : [];
};

const mbps = (v) => (!has(v) ? '–' : v >= 100 ? String(Math.round(v)) : v.toFixed(1));

// One meter: a figure with its unit, a bar of the load and the server's grade as a level.
function meter(key, label, icon, help, value, unit, pct, grade) {
  return {
    key, label, icon, help, value, unit, pct: has(pct) ? Math.min(100, Math.max(0, pct)) : null,
    level: levelOfGrade(grade),
    aria: `${label} ${has(pct) ? `${Math.round(pct)} percent` : 'unknown'}`
  };
}

// The last hour's download as the chart's data: {vals, top, cap (null when it is not drawn), topText, capText, caption}
// or {empty}. The scale is the hour's peak, so the shape of the traffic shows; the capacity is a dashed line only when
// the peak comes near enough for it to fit on the same scale, and is named in the caption otherwise (scaled to a 1 Gbps
// line, everyday traffic was a flat line at 0).
function chartOf(points, cap) {
  const vals = points.map((p) => p[1]);
  if (!vals.some(has)) return { empty: 'No WAN readings in the last hour yet.' };
  const peak = Math.max(...vals.filter(has));
  const showCap = !!cap && peak * 4 >= cap;
  const top = Math.max(showCap ? cap : 0, peak, 1);
  return {
    vals, top, cap: showCap ? cap : null,
    topText: `${mbps(top)} Mbps`,
    capText: showCap ? `capacity ${mbps(cap)} Mbps` : '',
    caption: `Download, last hour · peak ${mbps(peak)} Mbps${cap && !showCap ? ` · line capacity ${mbps(cap)} Mbps` : ''}`,
    aria: 'Download traffic, last hour'
  };
}

function wanOf(snap, line, history) {
  const of = (cap) => (cap ? `Mbps of ${mbps(cap)}` : 'Mbps');
  const s = snap || {};
  return {
    note: snap && !(line && line.down_mbps) ? 'line capacity not known yet' : 'vs line capacity',
    rx: meter('rx', 'Download', 'download', null, mbps(s.wan_rx_rate_mbps), of(line && line.down_mbps), s.wan_rx_load_pct, gradeOf(s, 'wan_rx')),
    tx: meter('tx', 'Upload', 'up', null, mbps(s.wan_tx_rate_mbps), of(line && line.up_mbps), s.wan_tx_load_pct, gradeOf(s, 'wan_tx')),
    chart: history ? chartOf(history.points, history.cap || (line && line.down_mbps) || null) : null
  };
}

// CPU and memory, each with its help.
const SYSTEM = [
  { key: 'cpu', label: 'CPU', help: 'router.cpu', about: 'About router cpu', field: 'cpu_usage_pct' },
  { key: 'mem', label: 'Memory', help: 'router.mem', about: 'About router memory', field: 'mem_usage_pct' }
];

function systemOf(snap) {
  const s = snap || {};
  const m = (d) => meter(d.key, d.label, null, { key: d.help, label: d.about }, has(s[d.field]) ? String(Math.round(s[d.field])) : '–', '%', s[d.field], gradeOf(s, d.key));
  return { cpu: m(SYSTEM[0]), mem: m(SYSTEM[1]) };
}

const BAND_COLORS = ['--s1', '--s2', '--s3', '--s4'];

// The clients: the online count, the split by band (wired and each Wi-Fi band) as a bar and a legend. The counts come
// from the live summary (the server sends no client row there; the list itself is fetched only by the Local network
// dialog).
function clientsOf(snap) {
  const c = snap && snap.clients;
  const out = { count: c ? String(c.online) : has(snap && snap.client_count) ? String(snap.client_count) : '–', bands: [], aria: '' };
  if (!c) return out;
  const bands = [['Wired', c.wired], ...Object.keys(c.by_band).sort().map((b) => [b, c.by_band[b]])].filter(([, n]) => n > 0);
  out.bands = bands.map(([name, n], k) => ({ name, n, color: BAND_COLORS[k % 4], title: `${name}: ${n}` }));
  out.aria = bands.map(([n, v]) => `${v} ${n}`).join(', ');
  return out;
}

// The mesh nodes with the number of clients on each; null when the router reports none. `{nodes: []}` before the first
// client summary has come.
function meshOf(snap) {
  const c = snap && snap.clients;
  if (!c) return { count: '', nodes: [] };
  if (!c.nodes.length) return null;
  const max = Math.max(1, ...c.nodes.map((n) => c.by_node[n.id] ?? n.clients ?? 0));
  return {
    count: `${c.nodes.length} · clients on each`, // the bars and numbers below are client counts
    nodes: c.nodes.map((n) => {
      const k = c.by_node[n.id] ?? n.clients ?? 0;
      const off = n.online === false;
      return {
        id: n.id,
        label: n.label,
        sub: [backhaulText(n), n.wired ? `${n.wired} wired` : ''].filter(Boolean).join(' · ') || 'mesh node',
        status: off ? { level: 'warn', word: 'Offline', icon: 'warn' } : null,
        clients: { action: 'openNetworkModalAt', arg: n.id, label: `Show the ${k} clients on ${n.label}`, count: String(k), title: `${k} client${k === 1 ? '' : 's'} online`, pct: (k / max) * 100 }
      };
    })
  };
}

// The view model's parts for a reading. `caps`: which parts the device offers {wan, sys, clients, mesh}; `snap`: the
// device's snapshot; `line`: the live line capacity {down_mbps, up_mbps}; `history`: {points: [[time, Mbps]], cap} of the
// last hour or null; `notice`: the sentence about the missing client list, or null. Reads only its arguments.
export function present({ caps, snap, line, history, notice }) {
  return {
    error: null,
    dim: false,
    notice: notice || null,
    wan: caps.wan ? wanOf(snap, line, history) : null,
    system: caps.sys ? systemOf(snap) : null,
    clients: caps.clients ? clientsOf(snap) : null,
    mesh: caps.mesh ? meshOf(snap) : null
  };
}

// Made-up devices for the parity test of the card's views (tests/ui/steps/core.mjs): a router with every part, and
// (fixture.variants) the least a router driver can offer, the system readings alone.
const FX_SNAP = {
  status: 'healthy', timestamp: 1767225600, wan_rx_rate_mbps: 123.4, wan_tx_rate_mbps: 5.2, wan_rx_load_pct: 12.3, wan_tx_load_pct: 10.4,
  cpu_usage_pct: 23.4, mem_usage_pct: 71.2, grades: { wan_rx: 'good', wan_tx: 'warn', cpu: 'good', mem: 'warn' },
  client_count: 5,
  clients: {
    online: 5, wired: 2, by_band: { '2.4 GHz': 1, '5 GHz': 2 }, by_node: { fx_main: 3, fx_hall: 2 },
    nodes: [{ id: 'fx_main', label: 'Example-Mesh Main', clients: 3, online: true, wired: 2 },
      { id: 'fx_hall', label: 'Example-Mesh Hall', clients: 2, online: false, backhaul: 'wifi', backhaul_band: '5 GHz', backhaul_rssi_dbm: -61 }]
  }
};

export function fixture() {
  const points = Array.from({ length: 12 }, (_, i) => [1767222000 + i * 300, i === 5 ? null : 20 + i * 8]);
  return present({
    caps: { wan: true, sys: true, clients: true, mesh: true }, snap: FX_SNAP, line: { down_mbps: 500, up_mbps: 50 },
    history: { points, cap: 500 }, notice: null
  });
}

fixture.variants = [() => present({ caps: { wan: false, sys: true, clients: false, mesh: false }, snap: FX_SNAP, line: null, history: null,
  notice: "Client list: this router's driver does not offer one." })];

export function mountRouter(section, ctx, device) {
  const { root, refs } = cloneTemplate('tpl-router');
  const offers = (c) => device.offers.includes(c);
  const lists = providers(ctx, 'clients').includes(device.id);
  const mesh = lists && providers(ctx, 'mesh').includes(device.id);
  const caps = { wan: offers('wan_counters'), sys: offers('router_system'), clients: lists, mesh };
  let notice = null;
  root.dataset.size = caps.wan || mesh ? '8' : '4';
  setText(refs.title, deviceName(device));
  setText(refs.meta, [device.model && device.host ? device.host : null, device.kind].filter(Boolean).join(' · '));
  if (caps.sys && !lists) {
    const clients = ctx.features.find((f) => f.feature === 'clients');
    // "none": no device offers it, here that means this router's driver has no client list
    notice = clients && clients.reason_kind === 'none'
      ? "Client list: this router's driver does not offer one." : ctx.reason(['clients'], 'Client list');
  }
  refs.history.hidden = !(caps.wan || caps.sys);
  refs.history.dataset.arg = device.id;
  // the Local network view is about the one network: on the card of the device that lists the clients, else the
  // primary router's
  const lanOwner = clientsDevice() || ctx.primary('wan_counters') || ctx.primary('router_system');
  refs.lan.hidden = !(topologyOffered() && lanOwner && lanOwner.id === device.id);
  placeCard(section, root, refs, device);
  const card = createCard({ panel: 'router', root: root.querySelector('.panel-b'), standard, fixture });

  let line = null; // the live line capacity {down_mbps, up_mbps}
  let snap = null;
  let history = null; // the last hour of WAN traffic {points, cap}
  let parts = null; // the view model of the last reading: kept while the device reports an error
  const pushed = partPusher(card);
  const push = () => pushed({ ...(parts || {}), error: errorOf(snap, device), dim: !!snap && snap.status === 'error' });
  let reading = true; // the snapshot holds a reading (not an error, not "not set up")
  const rebuild = () => { if (reading) parts = present({ caps, snap, line, history, notice }); };
  const paint = (s) => {
    snap = s;
    reading = paintCommon(root, refs, device, snap, null);
    rebuild();
    push();
  };
  parts = present({ caps, snap: null, line, history, notice });
  push();
  watchDevice(ctx.bus, device, paint);
  ctx.bus.subscribe('line', (l) => { line = l; if (snap) paint(snap); });

  // The last hour of WAN traffic: REST, refreshed every minute.
  const loadWan = async () => {
    const end = Date.now() / 1000;
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/history?start_time=${end - 3600}&end_time=${end}&max_points=60`);
    if (!res.ok) return;
    const body = await res.json();
    const i = body.fields.indexOf('rx_avg');
    history = { points: body.rows.map((r) => [r[0], r[i]]), cap: (body.capacity && body.capacity.down_mbps) || null };
    rebuild();
    push();
  };
  const refresh = () => {
    if (caps.wan) loadWan().catch(() => {});
  };
  refresh();
  setInterval(refresh, REFRESH_MS);
}
