// The DOCSIS line's detail view (the Channels dialog), for one line device at a time: the three range gauges, an alert
// when the server grades the new errors as a problem, every channel with its power, SNR and errors coloured by the
// server's per-channel grades, and the History tab (channel-history.js). The full reading comes from
// GET /api/devices/{id} (the live message carries no channel detail), refreshed every 30 s while the dialog is open.

import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { badge, closeModal, escapeHtml, openModal } from '../../js/util.js';
import { levelOfGrade, rangeOf, worstGrade } from '../../js/grades.js';
import { DEFAULT_TITLE, errorsText, gaugeMarkup, goodText, spreadOf } from './docsis.js';
import { barChartMarkup } from './channel-charts.js';
import { initChannelHistory, setChannelDevice, showChannelsTab } from './channel-history.js';

const REFRESH_MS = 30000;
let device = null;
let timer = 0;
let seq = 0;
let lastSnap = null;
const has = (v) => v !== null && v !== undefined;
const isOpen = () => document.getElementById('channelsModal').classList.contains('active');
const ink = (grade) => (grade === 'critical' ? 'ink-crit' : grade === 'warn' ? 'ink-warn' : '');

function row(c) {
  const g = c.grades || {};
  const worst = worstGrade(Object.values(g));
  const recent = c.uncorrectables_recent || 0;
  const word = recent > 0 ? `+${recent.toLocaleString()} errors` : worst === 'good' || !worst ? 'Locked' : 'Out of range';
  const down = c.direction === 'downstream';
  return `<tr${recent > 0 ? ' class="has-errors"' : ''}>
    <td class="lead"><span class="primary">${down ? 'Down' : 'Up'} ${c.channel_id}</span><span class="sub">${has(c.frequency_mhz) ? `${c.frequency_mhz} MHz` : '-'}${c.modulation ? ` · ${escapeHtml(c.modulation)}` : ''}</span></td>
    <td class="aside">${badge(levelOfGrade(worst || 'good'), word, 'quiet')}</td>
    <td data-l="Power" class="r num ${ink(g.power)}">${has(c.power_dbmv) ? `${c.power_dbmv} dBmV` : '-'}</td>
    <td data-l="SNR" class="r num ${ink(g.snr)}">${has(c.snr_db) ? `${c.snr_db} dB` : '-'}</td>
    <td data-l="Corrected" class="r num hide-narrow">${(c.correcteds || 0).toLocaleString()}</td>
    <td data-l="New" class="r num ${ink(recent > 0 ? g.uncorrectables : null)}">${recent > 0 ? `+${recent.toLocaleString()}` : '0'}</td>
    <td data-l="Since boot" class="r num">${(c.uncorrectables || 0).toLocaleString()}</td></tr>`;
}

// The three bar charts (downstream power and SNR, upstream power): the zone from the device's ranges, each bar's colour
// from the channel's own grades.
function renderCharts(snap) {
  const channels = snap.channels || [];
  const down = channels.filter((c) => c.direction === 'downstream').sort((a, b) => a.channel_id - b.channel_id);
  const up = channels.filter((c) => c.direction === 'upstream').sort((a, b) => a.channel_id - b.channel_id);
  const width = document.getElementById('channelsGauges').clientWidth;
  const draw = (id, noteId, list, key, field, rangeKey, unit, label) => {
    const range = rangeOf(device, rangeKey);
    const zone = range ? goodText(range) : '';
    document.getElementById(noteId).textContent = `${unit}${zone ? `, ${zone} (shaded)` : ''}`;
    document.getElementById(id).innerHTML = list.length
      ? barChartMarkup({ channels: list, value: (c) => c[field], grade: (c) => (c.grades || {})[key], range, unit, label, width, zoneNote: zone })
      : '<p class="muted">No channel readings at the moment.</p>';
  };
  draw('channelsPowerChart', 'channelsPowerNote', down, 'power', 'power_dbmv', 'ds_power', 'dBmV', 'Power per downstream channel');
  draw('channelsSnrChart', 'channelsSnrNote', down, 'snr', 'snr_db', 'ds_snr', 'dB', 'SNR per downstream channel');
  document.getElementById('channelsUpSection').hidden = !up.length;
  if (up.length) draw('channelsUpChart', 'channelsUpNote', up, 'power', 'power_dbmv', 'us_power', 'dBmV', 'Power per upstream channel');
}

function render(snap) {
  lastSnap = snap;
  const tbody = document.getElementById('channelsTableBody');
  const alert = document.getElementById('channelsAlertBox');
  const channels = snap.channels || [];
  const down = channels.filter((c) => c.direction === 'downstream');
  const up = channels.filter((c) => c.direction === 'upstream');
  document.getElementById('channelsSummaryBadge').textContent = `${device.model ? String(device.model).replace(/_/g, ' ') : DEFAULT_TITLE} · DOCSIS · ${down.length} down · ${up.length} up locked`;
  document.getElementById('channelsGauges').innerHTML = [
    ['Downstream power', 'ds_power', snap.downstream_power_avg, spreadOf(channels, 'downstream', 'power_dbmv')],
    ['Downstream SNR', 'ds_snr', snap.downstream_snr_avg, spreadOf(channels, 'downstream', 'snr_db')],
    ['Upstream power', 'us_power', snap.upstream_power_avg, spreadOf(channels, 'upstream', 'power_dbmv')]
  ].map(([name, key, v, spread]) => gaugeMarkup(name, rangeOf(device, key), v, snap.grades && snap.grades[key], spread)).join('');
  const graded = snap.grades && snap.grades.uncorrectables;
  const e = errorsText(snap, device);
  alert.hidden = !(graded === 'warn' || graded === 'critical');
  if (!alert.hidden) {
    alert.className = `empty st st-${levelOfGrade(graded)} compact`;
    alert.innerHTML = `<div><b>${escapeHtml(e.text)}</b>${escapeHtml(e.detail)}</div>`;
  }
  renderCharts(snap);
  if (!channels.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">No channel readings at the moment.</td></tr>';
    return;
  }
  const rank = { critical: 2, warn: 1, good: 0 };
  const sorted = channels.slice().sort((a, b) => {
    const ga = rank[worstGrade(Object.values(a.grades || {}))] || 0;
    const gb = rank[worstGrade(Object.values(b.grades || {}))] || 0;
    if (gb !== ga) return gb - ga;
    const ra = a.uncorrectables_recent || 0;
    const rb = b.uncorrectables_recent || 0;
    if (rb !== ra) return rb - ra;
    if (a.direction !== b.direction) return a.direction === 'downstream' ? -1 : 1;
    return a.channel_id - b.channel_id;
  });
  tbody.innerHTML = sorted.map(row).join('');
}

async function load() {
  const mine = ++seq;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const snap = await res.json();
    if (mine === seq) render(snap);
  } catch (e) {
    if (mine === seq) document.getElementById('channelsTableBody').innerHTML = `<tr><td colspan="7" class="muted">Could not read the channels: ${escapeHtml(e.message)}</td></tr>`;
  }
}

export function openChannelsModal(deviceId) {
  device = state.devices.find((d) => d.id === deviceId) || null;
  if (!device) return;
  setChannelDevice(device.id);
  document.getElementById('channelsTabs').hidden = false;
  showChannelsTab('current');
  openModal('channelsModal');
  document.querySelector('#channelsModal .m-close').focus(); // keyboard users land inside the dialog
  load();
  clearInterval(timer);
  timer = setInterval(() => {
    if (!isOpen()) { clearInterval(timer); return; }
    load();
  }, REFRESH_MS);
}

initChannelHistory();

// The charts are drawn at the dialog's width: redraw when it changes (a rotated phone, a resized window).
let lastWidth = 0;
new ResizeObserver(() => {
  const w = document.getElementById('channelsGauges').clientWidth;
  if (lastSnap && isOpen() && w && Math.abs(w - lastWidth) > 8) { lastWidth = w; renderCharts(lastSnap); }
}).observe(document.getElementById('channelsGauges'));

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && isOpen()) closeModal('channelsModal');
});

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openChannelsModal: (el) => openChannelsModal(el.dataset.arg)
});
