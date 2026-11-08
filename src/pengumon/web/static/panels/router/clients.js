// The Local network modal: the topology at the top (topology.js, fed with the answers loaded here) and, when the
// `clients` feature is available (devices.js), the devices connected to the network (from /api/devices/{id}/clients), a
// table to sort and filter, and the number of clients over time (/api/devices/{id}/clients/history). Refreshes every
// minute while open; nothing goes through the websocket.
//
// What the server hides (names, IP and MAC addresses under privacy.client_details) is simply absent from the rows. Every
// value is shown through textContent. The Wi-Fi link rate is not traffic and is labeled so.

import { registerActions } from '../../js/dispatch.js';
import { CLIENT_KINDS, keepKeyboard, openModal, deviceName, localFormat } from '../../js/util.js';
import { clientsDevice, setTopologyClients, setTopologyFilter } from './topology.js';
import { series } from '../../js/chart-theme.js';
import { levelOfGrade } from '../../js/grades.js';

const REFRESH_MS = 60000;
const color = (i) => series(i);

const view = {
  data: null, // the last /clients answer
  history: null, // the last /clients/history answer
  sortKey: 'label',
  sortDir: 1,
  showOffline: false,
  filter: null, // null, or { kind: 'node' | 'band', value }: what the table is narrowed to
  rangeSeconds: 86400,
  chartMode: 'band',
  chart: null,
  timer: null
};

const modal = () => document.getElementById('networkModal');
const deviceUrl = () => `/api/devices/${encodeURIComponent(clientsDevice().id)}`;

function make(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = text;
  if (className) el.className = className;
  return el;
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours < 48 ? `${hours}h ${minutes}m` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'i sm');
  svg.setAttribute('aria-hidden', 'true');
  const use = svg.appendChild(document.createElementNS('http://www.w3.org/2000/svg', 'use'));
  use.setAttribute('href', `#i-${name}`);
  return svg;
}

function nodeLabel(id) {
  const node = view.data && view.data.nodes.find(n => n.id === id);
  return node ? node.label : (id || '-');
}

// Opens the modal; `nodeId` (optional) filters the client table to one mesh node.
export function openNetworkModal(nodeId) {
  const lists = !!clientsDevice();
  document.querySelectorAll('#networkModal .clients-only').forEach((el) => { el.hidden = !lists; });
  document.getElementById('clientsEmpty').hidden = lists;
  document.getElementById('clientsUpdated').textContent = '';
  keepKeyboard(document.getElementById('networkModal'));
  openModal('networkModal');
  if (nodeId !== undefined) view.filter = nodeId ? { kind: 'node', value: nodeId } : null;
  setTopologyClients(view.data);
  setTopologyFilter(view.filter && view.filter.kind === 'node' ? view.filter.value : null);
  if (!clientsDevice()) return;
  loadClients();
  loadHistory();
  clearInterval(view.timer);
  view.timer = setInterval(() => {
    if (!modal().classList.contains('active')) {
      clearInterval(view.timer);
      view.timer = null;
      return;
    }
    loadClients();
    loadHistory();
  }, REFRESH_MS);
}

async function loadClients() {
  try {
    const res = await fetch(`${deviceUrl()}/clients`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    view.data = await res.json();
    setTopologyClients(view.data);
    renderClients();
  } catch (e) {
    const body = document.getElementById('clientsBody');
    body.replaceChildren();
    const td = make('td', `Could not load the clients: ${e.message}`, 'clients-empty');
    td.colSpan = 6;
    body.appendChild(make('tr')).appendChild(td);
  }
}

async function loadHistory() {
  const end = Math.floor(Date.now() / 1000);
  try {
    const res = await fetch(`${deviceUrl()}/clients/history?start_time=${end - view.rangeSeconds}&end_time=${end}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    view.history = await res.json();
    renderChart();
  } catch (e) {
    document.getElementById('clientsChartNote').textContent = `Could not load the history: ${e.message}`;
  }
}

function chip(text, action, arg, active) {
  const el = make(action ? 'button' : 'span', text, 'chip clients-chip');
  if (action) {
    el.type = 'button';
    el.setAttribute('aria-pressed', String(!!active));
    el.dataset.action = action;
    el.dataset.arg = arg;
  }
  return el;
}

const isFilter = (kind, value) => !!view.filter && view.filter.kind === kind && view.filter.value === value;

function renderChips(data) {
  const chips = document.getElementById('clientsChips');
  chips.replaceChildren();
  const s = data.summary;
  chips.appendChild(chip(`${s.online} online`, 'clearClientsFilter', '', !view.filter));
  chips.appendChild(chip(`${s.wired} wired`, 'filterClientsBand', 'wired', isFilter('band', 'wired')));
  Object.keys(s.by_band).sort().forEach(band => chips.appendChild(chip(`${s.by_band[band]} on ${band}`, 'filterClientsBand', band, isFilter('band', band))));
  data.nodes.forEach(node => {
    const n = s.by_node[node.id] || 0;
    chips.appendChild(chip(`${node.label}: ${n}`, 'filterClientsNode', node.id, isFilter('node', node.id)));
  });
}

const bandOf = (c) => (c.connection === 'wired' ? 'wired' : c.band);

function visibleClients(data) {
  const f = view.filter;
  return data.clients.filter(c => (view.showOffline || c.online)
    && (!f || (f.kind === 'node' ? c.node === f.value : bandOf(c) === f.value)));
}

function sortValue(c, key) {
  if (key === 'connection') return c.connection === 'wired' ? 'wired' : `wifi ${c.band || ''}`;
  if (key === 'node') return c.node ? nodeLabel(c.node).toLowerCase() : null;
  const v = c[key];
  return typeof v === 'string' ? v.toLowerCase() : v;
}

function sorted(list) {
  const key = view.sortKey;
  return list.slice().sort((a, b) => {
    const x = sortValue(a, key);
    const y = sortValue(b, key);
    if ((x === null || x === undefined) !== (y === null || y === undefined)) return x === null || x === undefined ? 1 : -1;
    if (x === y || x === null || x === undefined) return 0;
    return (x < y ? -1 : 1) * view.sortDir;
  });
}

function row(c, hasNodes) {
  const tr = make('tr', null, c.online ? '' : 'offline');
  const name = tr.appendChild(make('td', null, 'lead'));
  const who = name.appendChild(make('span', null, 'cl-name'));
  const kind = CLIENT_KINDS[c.kind]; // [label, icon] for a device type the router knows, else the connection's icon
  who.appendChild(icon(kind ? kind[1] : (c.connection === 'wired' ? 'plug' : 'host')));
  const text = who.appendChild(make('span'));
  text.appendChild(make('span', c.label, 'primary'));
  const detail = [kind ? kind[0] : null, c.ip, c.mac].filter(Boolean).join(' · ');
  if (detail) text.appendChild(make('span', detail, 'sub mono'));
  const conn = tr.appendChild(make('td', null, 'aside')).appendChild(make('span', null, 'chip'));
  conn.appendChild(icon(c.connection === 'wired' ? 'plug' : 'wifi'));
  conn.append(c.connection === 'wired' ? 'Wired' : (c.band || 'Wi-Fi'));
  if (hasNodes) tr.appendChild(make('td', c.node ? nodeLabel(c.node) : '-')).dataset.l = 'Node';
  const signal = tr.appendChild(make('td'));
  signal.dataset.l = '';
  if (c.signal) {
    const s = c.signal; // word, bars and grade come from the server (grades.py signal_grade)
    const sig = signal.appendChild(make('span', null, `sig st-${levelOfGrade(s.grade)}`));
    const bars = sig.appendChild(make('span', null, 'bars'));
    for (let b = 1; b <= 4; b++) bars.appendChild(make('i', null, b <= s.bars ? 'on' : ''));
    sig.append(s.word, ' ');
    sig.appendChild(make('span', `${c.rssi_dbm} dBm`, 'w num'));
  } else {
    signal.appendChild(make('span', c.connection === 'wired' ? 'wired' : '-', 'muted'));
  }
  const speed = tr.appendChild(make('td', c.link_tx_mbps === null || c.link_tx_mbps === undefined
    ? '-' : `${Math.round(c.link_tx_mbps)} / ${Math.round(c.link_rx_mbps ?? 0)} Mbps`, 'num'));
  speed.dataset.l = 'Link';
  tr.appendChild(make('td', formatDuration(c.connected_s), 'num')).dataset.l = 'Connected';
  return tr;
}

function renderClients() {
  const data = view.data;
  if (!data) return;
  const hasNodes = data.nodes.length > 0;
  if (view.filter && view.filter.kind === 'node' && !data.nodes.some(n => n.id === view.filter.value)) view.filter = null;
  setTopologyFilter(view.filter && view.filter.kind === 'node' ? view.filter.value : null);

  const owner = clientsDevice();
  const asOf = data.ts ? `as of ${localFormat(data.ts * 1000, { hour: '2-digit', minute: '2-digit' })}` : 'waiting for the first client list';
  document.getElementById('clientsUpdated').textContent = [owner ? deviceName(owner) : null, asOf].filter(Boolean).join(' · ');
  const note = document.getElementById('clientsPrivacyNote');
  note.hidden = !data.privacy.hint;
  note.textContent = data.privacy.hint || '';
  renderChips(data);

  document.getElementById('clientsNodeHead').style.display = hasNodes ? '' : 'none';
  document.getElementById('clientsNodeModeBtn').style.display = hasNodes ? '' : 'none';
  const filter = document.getElementById('clientsNodeFilter');
  filter.textContent = view.filter
    ? `Showing only ${view.filter.kind === 'node' ? nodeLabel(view.filter.value) : (view.filter.value === 'wired' ? 'wired devices' : view.filter.value)} (press its chip again, or "online", to clear)` : '';
  document.querySelectorAll('.clients-sort').forEach(b => {
    const sorted = b.dataset.arg === view.sortKey;
    b.classList.toggle('sorted', sorted);
    const th = b.closest('th');
    if (sorted) th.setAttribute('aria-sort', view.sortDir > 0 ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort');
    b.querySelector('svg')?.remove();
    if (sorted) b.appendChild(icon(view.sortDir > 0 ? 'up' : 'down'));
  });

  const body = document.getElementById('clientsBody');
  body.replaceChildren();
  const list = sorted(visibleClients(data));
  if (!list.length) {
    const td = make('td', data.ts ? 'No devices to show.' : 'No client list yet.', 'clients-empty');
    td.colSpan = hasNodes ? 6 : 5;
    body.appendChild(make('tr')).appendChild(td);
  }
  list.forEach(c => body.appendChild(row(c, hasNodes)));
}

function timeLabel(ts) {
  const hm = localFormat(ts * 1000, { hour: '2-digit', minute: '2-digit' });
  return view.rangeSeconds > 86400 ? `${localFormat(ts * 1000, { weekday: 'short' })} ${hm}` : hm;
}

function seriesFor(history) {
  const points = history.points;
  const series = (label, pick) => ({ label, data: points.map(pick) });
  if (view.chartMode === 'node' && history.nodes.length) {
    return history.nodes.map(n => series(n.label, p => p.by_node[n.id] ?? 0));
  }
  const bands = [...new Set(points.flatMap(p => Object.keys(p.by_band)))].sort().reverse(); // 5 GHz first
  const all = [...bands.map(b => series(b, p => p.by_band[b] ?? 0)), series('Wired', p => p.wired)];
  const used = all.filter(set => set.data.some(v => v > 0)); // a band with no client in the range is not drawn
  return used.length ? used : all.slice(-1);
}

function renderChart() {
  const history = view.history;
  if (!history) return;
  if (view.chartMode === 'node' && !history.nodes.length) view.chartMode = 'band';
  const note = document.getElementById('clientsChartNote');
  note.textContent = history.points.length
    ? `One point per ${formatDuration(history.bucket_seconds)} (mean of the readings in it)`
    : 'No client counts stored for this range yet.';
  const sets = seriesFor(history);
  const legend = document.getElementById('clientsLegend');
  legend.replaceChildren();
  sets.forEach((set, i) => {
    const item = legend.appendChild(make('span'));
    item.appendChild(make('i', null, 'key')).style.setProperty('--c', `var(--s${(i % 4) + 1})`);
    item.append(set.label);
  });
  const datasets = sets.map((s, i) => ({
    ...s,
    borderColor: color(i),
    backgroundColor: `${color(i)}b0`,
    fill: true,
    pointRadius: 0,
    borderWidth: 1.5,
    tension: 0.2
  }));
  const data = { labels: history.points.map(p => timeLabel(p.ts)), datasets };
  if (view.chart) {
    view.chart.data = data;
    view.chart.update();
    return;
  }
  view.chart = new Chart(document.getElementById('clientsChart'), {
    type: 'line',
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 8, maxRotation: 0, font: { size: 10 } } },
        y: { stacked: true, min: 0, grid: {}, ticks: { precision: 0, font: { size: 10 }, maxTicksLimit: 5 } }
      },
      plugins: { legend: { display: false } }
    }
  });
}

function markActive(containerId, arg) {
  document.querySelectorAll(`#${containerId} button`).forEach(b => b.setAttribute('aria-pressed', String(b.dataset.arg === String(arg))));
}

export function setClientsRange(seconds) {
  view.rangeSeconds = seconds;
  markActive('clientsRangeButtons', seconds);
  loadHistory();
}

export function setClientsChartMode(mode) {
  view.chartMode = mode;
  markActive('clientsModeButtons', mode);
  renderChart();
}

export function sortClients(key) {
  view.sortDir = view.sortKey === key ? -view.sortDir : 1;
  view.sortKey = key;
  renderClients();
}

export function toggleOfflineClients(checked) {
  view.showOffline = checked;
  renderClients();
}

// Sets (not toggles) the node filter; an empty id clears it (the topology's mesh nodes and lone pill).
export function setClientsNodeFilter(nodeId) {
  view.filter = nodeId ? { kind: 'node', value: nodeId } : null;
  renderClients();
}

export function filterClientsNode(nodeId) {
  view.filter = isFilter('node', nodeId) ? null : { kind: 'node', value: nodeId };
  renderClients();
}

export function filterClientsBand(band) {
  view.filter = isFilter('band', band) ? null : { kind: 'band', value: band };
  renderClients();
}

// Chart.js keeps the colors it resolved when a chart was made, so a theme change draws the chart again.
document.addEventListener('themechange', () => {
  if (!view.chart) return;
  view.chart.destroy();
  view.chart = null;
  renderChart();
});

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openNetworkModal: () => openNetworkModal(),
  openNetworkModalAt: (el) => openNetworkModal(el.dataset.arg),
  setClientsNodeFilter: (el) => setClientsNodeFilter(el.dataset.arg),
  sortClients: (el) => sortClients(el.dataset.arg),
  filterClientsNode: (el) => filterClientsNode(el.dataset.arg),
  filterClientsBand: (el) => filterClientsBand(el.dataset.arg),
  clearClientsFilter: () => setClientsNodeFilter(''),
  setClientsRange: (el) => setClientsRange(Number(el.dataset.arg)),
  setClientsChartMode: (el) => setClientsChartMode(el.dataset.arg)
});
registerActions('change', {
  toggleOfflineClients: (el) => toggleOfflineClients(el.checked)
});
