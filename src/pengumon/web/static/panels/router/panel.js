// Router: one card per enabled device offering wan_counters or router_system (two routers, two cards). Each block only
// when the device offers what it shows: WAN traffic against the line capacity (wan_counters), CPU and memory
// (router_system), the clients by band and the mesh nodes (when the device is a provider of the `clients` / `mesh`
// features). Colours are the server's grades; the capacity and the load are the server's too (the live `line` part and
// the snapshot's wan_*_load_pct).

import { backhaulText, cloneTemplate, deviceName, escapeHtml, icon, setText } from '../../js/util.js';
import { watchDevice } from '../../js/device-feed.js';
import { paintCommon, placeCard } from '../../js/device-panel.js';
import { gradeOf, levelOfGrade } from '../../js/grades.js';
import { initTopology, clientsDevice, topologyOffered } from './topology.js';
import './clients.js';
import './history.js';
import './help.js';

const has = (v) => v !== null && v !== undefined;
const REFRESH_MS = 60000;
const BAND_COLORS = ['--s1', '--s2', '--s3', '--s4'];
const providers = (ctx, feature) => {
  const f = ctx.features.find((x) => x.feature === feature);
  return f && f.available ? f.provided_by || [] : [];
};

const mbps = (v) => (!has(v) ? '–' : v >= 100 ? String(Math.round(v)) : v.toFixed(1));
const meterColor = (grade) => ({ warn: 'var(--warn)', crit: 'var(--crit)' }[levelOfGrade(grade)] || 'var(--accent)');

function meterRefs(el) {
  return { el, v: el.querySelector('[data-f=v]'), of: el.querySelector('[data-f=of]'), fill: el.querySelector('.fill'), track: el.querySelector('.track') };
}

function paintMeter(m, label, value, pct, grade, suffix) {
  setText(m.v, value);
  if (suffix !== undefined) setText(m.of, suffix);
  m.el.style.setProperty('--mc', meterColor(grade));
  m.fill.style.width = `${has(pct) ? Math.min(100, Math.max(0, pct)) : 0}%`;
  m.track.setAttribute('aria-label', `${label} ${has(pct) ? `${Math.round(pct)} percent` : 'unknown'}`);
}

// A small area chart of the last hour's download (SVG, token colours). The scale is the hour's peak, so the shape of
// the traffic shows; the capacity is drawn as a dashed line only when the peak comes near enough for it to fit on the
// same scale, and is named in the caption otherwise (scaled to a 1 Gbps line, everyday traffic was a flat line at 0).
function wanChartSvg(points, cap) {
  const vals = points.map((p) => p[1]);
  if (!vals.some(has)) return '<p class="fine">No WAN readings in the last hour yet.</p>';
  const W = 320;
  const H = 70;
  const L = 60;
  const peak = Math.max(...vals.filter(has));
  const showCap = cap && peak * 4 >= cap;
  const top = Math.max(showCap ? cap : 0, peak, 1);
  const max = top * 1.08;
  const x = (i) => L + (i / Math.max(1, vals.length - 1)) * (W - L - 4);
  const y = (v) => 6 + (1 - v / max) * (H - 18);
  let d = '';
  let pen = false;
  vals.forEach((v, i) => {
    if (!has(v)) { pen = false; return; }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
  });
  const first = vals.findIndex(has);
  let last = vals.length - 1;
  while (!has(vals[last])) last--;
  const capLine = showCap ? `<line x1="${L}" x2="${W - 4}" y1="${y(cap).toFixed(1)}" y2="${y(cap).toFixed(1)}" stroke="var(--ink-3)" stroke-dasharray="4 4"/><text x="${L + 4}" y="${(y(cap) - 4).toFixed(1)}" style="fill:var(--ink-2)">capacity ${mbps(cap)} Mbps</text>` : '';
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Download traffic, last hour">
    <line class="base" x1="${L}" x2="${W - 4}" y1="${y(0)}" y2="${y(0)}"/><text x="${L - 6}" y="${y(0) + 3}" text-anchor="end">0</text><text x="${L - 6}" y="${y(top) + 3}" text-anchor="end">${mbps(top)} Mbps</text>
    ${capLine}<path d="${d}L${x(last).toFixed(1)} ${y(0)}L${x(first).toFixed(1)} ${y(0)}Z" fill="var(--s1)" opacity=".18"/><path d="${d}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(last).toFixed(1)}" cy="${y(vals[last]).toFixed(1)}" r="3.5" fill="var(--s1)" stroke="var(--surface-1)" stroke-width="2"/>
    <text x="${L}" y="${H - 1}">60 min ago</text><text x="${W - 4}" y="${H - 1}" text-anchor="end">now</text></svg>
    <p class="fine">Download, last hour · peak ${mbps(peak)} Mbps${cap && !showCap ? ` · line capacity ${mbps(cap)} Mbps` : ''}</p></div>`;
}

function mountRouter(section, ctx, device) {
  const { root, refs } = cloneTemplate('tpl-router');
  const offers = (c) => device.offers.includes(c);
  const lists = providers(ctx, 'clients').includes(device.id);
  const mesh = lists && providers(ctx, 'mesh').includes(device.id);
  refs.wan.hidden = !offers('wan_counters');
  refs.sys.hidden = !offers('router_system');
  refs.clients.hidden = !lists;
  refs.mesh.hidden = !mesh;
  const blocks = [refs.wan, refs.sys, refs.clients, refs.mesh].filter((b) => !b.hidden).length;
  refs.grid.className = `rgrid n${blocks}`;
  root.dataset.size = offers('wan_counters') || mesh ? '8' : '4';
  setText(refs.title, deviceName(device));
  setText(refs.meta, [device.model && device.host ? device.host : null, device.driver].filter(Boolean).join(' · '));
  if (offers('router_system') && !lists) {
    refs.capNoteMissing.hidden = false;
    refs.capNoteMissing.querySelector('span').textContent = ctx.reason(['clients'], 'Client list');
  }
  refs.history.hidden = !(offers('wan_counters') || offers('router_system'));
  refs.history.dataset.arg = device.id;
  // the Local network view is about the one network: on the card of the device that lists the clients, else the
  // primary router's
  const lanOwner = clientsDevice() || ctx.primary('wan_counters') || ctx.primary('router_system');
  refs.lan.hidden = !(topologyOffered() && lanOwner && lanOwner.id === device.id);
  refs.count.disabled = !lists;
  placeCard(section, root, refs, device);
  const rx = meterRefs(refs.rx);
  const tx = meterRefs(refs.tx);
  const cpu = meterRefs(refs.cpu);
  const mem = meterRefs(refs.mem);

  let line = null; // the live line capacity {down_mbps, up_mbps}
  let last = null;
  const paint = (snap) => {
    last = snap;
    if (!paintCommon(root, refs, device, snap, null)) return;
    if (!refs.wan.hidden) {
      const down = snap.wan_rx_rate_mbps;
      const up = snap.wan_tx_rate_mbps;
      const of = (cap) => (cap ? ` Mbps of ${mbps(cap)}` : ' Mbps');
      paintMeter(rx, 'Download', mbps(down), snap.wan_rx_load_pct, gradeOf(snap, 'wan_rx'), of(line && line.down_mbps));
      paintMeter(tx, 'Upload', mbps(up), snap.wan_tx_load_pct, gradeOf(snap, 'wan_tx'), of(line && line.up_mbps));
      setText(refs.capNote, line && line.down_mbps ? 'vs line capacity' : 'line capacity not known yet');
    }
    if (!refs.sys.hidden) {
      paintMeter(cpu, 'CPU', has(snap.cpu_usage_pct) ? String(Math.round(snap.cpu_usage_pct)) : '–', snap.cpu_usage_pct, gradeOf(snap, 'cpu'));
      paintMeter(mem, 'Memory', has(snap.mem_usage_pct) ? String(Math.round(snap.mem_usage_pct)) : '–', snap.mem_usage_pct, gradeOf(snap, 'mem'));
    }
    if (lists && snap.clients) paintClients(snap.clients);
    else if (lists) setText(refs.countVal, has(snap.client_count) ? String(snap.client_count) : '–');
  };
  watchDevice(ctx.bus, device, paint);
  ctx.bus.subscribe('line', (l) => { line = l; if (last) paint(last); });

  // The last hour of WAN traffic: REST, refreshed every minute.
  const loadWan = async () => {
    const end = Date.now() / 1000;
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/history?start_time=${end - 3600}&end_time=${end}&max_points=60`);
    if (!res.ok) return;
    const body = await res.json();
    const i = body.fields.indexOf('rx_avg');
    const cap = (body.capacity && body.capacity.down_mbps) || (line && line.down_mbps) || null;
    refs.wanChart.innerHTML = wanChartSvg(body.rows.map((r) => [r[0], r[i]]), cap);
  };
  // The client breakdown and the mesh nodes: counts from the live summary (the server sends no client row there; the
  // list itself is fetched only by the Local network dialog).
  let clientsKey = '';
  const paintClients = (c) => {
    const key = JSON.stringify(c);
    if (key === clientsKey) return;
    clientsKey = key;
    const bands = [['Wired', c.wired], ...Object.keys(c.by_band).sort().map((b) => [b, c.by_band[b]])].filter(([, n]) => n > 0);
    refs.bands.innerHTML = bands.map(([n, v], k) => `<i style="flex:${v};--c:var(${BAND_COLORS[k % 4]})" title="${escapeHtml(n)}: ${v}"></i>`).join('');
    refs.bands.setAttribute('aria-label', bands.map(([n, v]) => `${v} ${n}`).join(', '));
    refs.bandLegend.innerHTML = bands.map(([n, v], k) => `<span><i class="key" style="--c:var(${BAND_COLORS[k % 4]})"></i>${escapeHtml(n)} <b class="num">${v}</b></span>`).join('');
    setText(refs.countVal, String(c.online));
    if (mesh) {
      refs.mesh.hidden = !c.nodes.length;
      refs.grid.className = `rgrid n${[refs.wan, refs.sys, refs.clients, refs.mesh].filter((b) => !b.hidden).length}`;
      const max = Math.max(1, ...c.nodes.map((n) => c.by_node[n.id] ?? n.clients ?? 0));
      setText(refs.meshCount, `${c.nodes.length} · clients on each`); // the bars and numbers below are client counts
      refs.nodes.innerHTML = c.nodes.map((n) => {
        const k = c.by_node[n.id] ?? n.clients ?? 0;
        const off = n.online === false;
        const sub = [backhaulText(n), n.wired ? `${n.wired} wired` : ''].filter(Boolean).join(' · ') || 'mesh node';
        return `<li${off ? ' class="st-warn"' : ''}><span class="n-ic">${icon('mesh')}</span><span class="n-id"><b>${escapeHtml(n.label)}</b><span>${escapeHtml(sub)}</span></span><span class="n-st">${off ? `${icon('warn')}Offline` : ''}</span>
          <button type="button" class="n-c" data-action="openNetworkModalAt" data-arg="${escapeHtml(n.id)}" aria-label="Show the ${k} clients on ${escapeHtml(n.label)}" title="${k} client${k === 1 ? '' : 's'} online"><b class="num">${k}</b><i style="width:${(k / max) * 100}%"></i></button></li>`;
      }).join('');
    }
  };
  const refresh = () => {
    if (!refs.wan.hidden) loadWan().catch(() => {});
  };
  refresh();
  setInterval(refresh, REFRESH_MS);
}

export default {
  id: 'router',
  title: 'Router',
  tab: 'network',
  icon: 'router',
  instances: (ctx) => ctx.devicesOffering('wan_counters', 'router_system').map((d) => ({ key: d.id, device: d })),
  available: (ctx) => ctx.devicesOffering('wan_counters', 'router_system').length > 0,
  reason: (ctx) => ctx.reason(['router_system', 'line_load'], 'Router telemetry'),
  prepare: (ctx) => initTopology(ctx),
  mount: (section, ctx, inst) => mountRouter(section, ctx, inst.device)
};
