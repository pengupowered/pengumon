// The traffic dialog of one device (opened from a row of the Internet use card): its download and upload over 24 hours or
// 7 days (per hour) or 30 days (per day), opening in the range the card shows, with buttons for the others; the totals, the
// busiest hour or day, its apps over the range (as the card counts them: the router's latest 24-hour window over 24 hours,
// the sum of the whole days over 7 and 30) and the apps of each day, from GET /api/devices/{id}/traffic/clients/{key}.
// The pencil by its name (shown where names are: privacy "full") gives it a name in Pengumon, shown instead of the router's
// (PUT /api/devices/{id}/clients/{key}/name); the card is asked again so its row says it too. Back, Escape and Done close it (js/modal.js); every value goes in through textContent.

import { registerActions } from '../../js/dispatch.js';
import { CLIENT_KINDS } from '../../js/device-text.js';
import { formatBytes } from '../../js/format.js';
import { formatClock } from '../../js/clock.js';
import { closeModal, keepKeyboard, openModal } from '../../js/modal.js';
import { RANGES, dayNotes, dayWords, daysOf, gapNotes, hoursOf, refreshTraffic, shownRange } from './model.js';
import { hoursChart, legendOf } from './chart.js';
import { appRow } from './view.js';
import { has } from '../../js/values.js';

const $ = (id) => document.getElementById(id);
const modal = () => $('trafficClientModal');

let chart = null;
let seq = 0;
let lastBody = null; // the answer drawn, for a redraw after a theme change
let shown = null; // {deviceId, key} of the device the dialog is open on
let range = '168'; // the range chosen in the dialog (a RANGES arg)
const chosen = () => RANGES.find((r) => r.arg === range) || RANGES[0];

const make = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

// "2026-11-09" (a calendar day, not an instant) -> "Mon 9 Nov"
function dayText(day) {
  const [y, m, d] = day.split('-').map(Number);
  return new Intl.DateTimeFormat([], { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short' }).format(Date.UTC(y, m - 1, d));
}

function stat(label, value, sub, key) {
  const s = make('div', 'stat');
  s.dataset.v = key;
  s.append(make('span', 'lbl', label), make('span', 'val num', value));
  if (sub) s.appendChild(make('span', 'sub', sub));
  return s;
}

function show(body) {
  lastBody = body;
  const c = body.client;
  const kind = CLIENT_KINDS[c.kind];
  $('trafficClientTitle').textContent = c.label;
  $('trafficClientMeta').textContent = [kind ? kind[0] : null, c.vendor, c.mac, c.router_label ? `the router calls it ${c.router_label}` : null].filter(Boolean).join(' · ');
  $('trafficClientRename').hidden = body.privacy !== 'full' || !$('trafficClientNameForm').hidden;
  const perDay = Array.isArray(body.daily);
  const span = chosen().title.toLowerCase(); // "last 7 days"
  const h = perDay ? daysOf(body.daily) : hoursOf(body.series, body.hours > 24);
  const rows = perDay ? body.daily : body.series;
  const busiest = rows.reduce((best, r) => (has(r.down) && (!best || r.down > best.down) ? r : best), null);
  const busy = busiest && busiest.down > 0;
  const stats = $('trafficClientStats');
  stats.replaceChildren(
    stat('Downloaded', formatBytes(body.totals.down), span, 'down'),
    stat('Uploaded', formatBytes(body.totals.up), span, 'up'),
    stat(perDay ? 'Busiest day' : 'Busiest hour', busy ? formatBytes(busiest.down) : '–',
      !busy ? 'no traffic' : perDay ? dayWords(busiest.day, true) : `${formatClock(busiest.hour * 1000, true)}-${formatClock((busiest.hour + 3600) * 1000)}`, 'busiest'),
    stat('Mostly', c.traffic_hint || '–', c.traffic_hint ? `at least 80 % of its traffic, ${body.apps_days ? span : 'last 24 hours'}` : 'no single app stands out', 'hint')
  );
  $('trafficClientPer').textContent = perDay ? 'Per day' : 'Per hour';
  $('trafficClientSpan').textContent = perDay ? `${span}, whole days; today is in 24 h` : span;
  const data = { ...h, label: `${c.label}: download and upload per ${perDay ? 'day' : 'hour'} over the ${span}, ${formatBytes(body.totals.down)} down, ${formatBytes(body.totals.up)} up.` };
  const canvas = $('trafficClientChart');
  if (!chart) chart = hoursChart(canvas);
  chart.update(data, true);
  $('trafficClientLegend').replaceChildren(...legendOf(data, canvas).map((k) => {
    const s = make('span', `trf-key${k.pattern ? ' gap' : ''}`);
    const sw = make('i', 'key');
    sw.style.setProperty('--c', k.color);
    sw.setAttribute('aria-hidden', 'true');
    s.append(sw, k.name);
    return s;
  }));
  $('trafficClientNotes').replaceChildren(...(perDay ? dayNotes(body.daily) : gapNotes(h)).map((n) => { const p = make('p', 'trf-note', n.text); p.dataset.v = n.key; return p; }));

  const end = body.apps_window_end;
  $('trafficClientAppsWindow').textContent = body.apps_days ? span
    : has(end) ? `the 24 h to ${formatClock(end * 1000, Date.now() / 1000 - end > 20 * 3600)}` : 'the router\'s latest 24 h';
  const apps = $('trafficClientApps');
  const max = body.apps.length ? Math.max(...body.apps.map((a) => a.down)) : 0;
  apps.replaceChildren(...(body.apps.length ? body.apps.map((a) => appRow({
    key: a.app, name: a.app, down: formatBytes(a.down), up: formatBytes(a.up), pct: max > 0 ? Math.max(2, Math.round((a.down / max) * 100)) : 0
  })) : [make('p', 'trf-empty', body.apps_days ? 'No whole day of app figures is stored for this device in this range yet.' : 'The router has no app figures for this device in its latest window.')]));

  const days = $('trafficClientDays');
  days.replaceChildren(...(body.days.length ? body.days.slice().reverse().map((d) => {
    const li = make('li', 'tcl-day');
    li.dataset.item = d.day;
    li.appendChild(make('b', 'tcl-date', dayText(d.day)));
    const list = make('span', 'tcl-apps');
    d.apps.slice(0, 5).forEach((a) => {
      const chip = make('span', 'chip', `${a.app} ${formatBytes(a.down)}`);
      chip.title = `${a.app}: ${formatBytes(a.down)} down, ${formatBytes(a.up)} up`;
      list.appendChild(chip);
    });
    if (d.apps.length > 5) list.appendChild(make('span', 'muted', `+${d.apps.length - 5} more`));
    if (!d.apps.length) list.appendChild(make('span', 'muted', 'no apps'));
    li.appendChild(list);
    li.title = `The router's totals for the 24 h to ${formatClock(d.window_end * 1000, true)}`;
    return li;
  }) : [make('li', 'trf-empty', 'No daily app history stored in this range yet: Pengumon keeps the router\'s count of each whole day, read just after midnight.')]));
  $('trafficClientStatus').hidden = true;
  $('trafficClientBody').hidden = false;
}

// The range buttons, the chosen one pressed.
function rangeButtons() {
  $('trafficClientRange').replaceChildren(...RANGES.map((r) => {
    const b = make('button', null, r.label);
    b.type = 'button';
    b.dataset.action = 'trafficClientRange';
    b.dataset.arg = r.arg;
    b.title = r.title;
    b.setAttribute('aria-pressed', String(r.arg === range));
    return b;
  }));
}

async function load() {
  const mine = ++seq;
  const { deviceId, key } = shown;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/traffic/clients/${encodeURIComponent(key)}?${chosen().query}`);
    if (mine !== seq) return;
    if (res.status === 404) return problem('The router has no traffic stored for this device.');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (mine === seq) show(body);
  } catch (e) {
    if (mine === seq) problem(`Could not load this device's traffic: ${e.message}`);
  }
}

// --- the name in Pengumon ---------------------------------------------------------------------------------------------

function nameError(text) {
  const e = $('trafficClientNameError');
  e.hidden = !text;
  e.textContent = text || '';
}

function startRename() {
  if (!lastBody) return;
  const c = lastBody.client;
  const routers = c.router_label || c.label;
  const input = $('trafficClientNameInput');
  input.value = c.router_label ? c.label : '';
  input.placeholder = routers;
  $('trafficClientNameClear').hidden = !c.router_label;
  $('trafficClientNameNote').textContent = `Shown instead of the router's name (${routers}) wherever Pengumon shows device names.`;
  nameError('');
  $('trafficClientRename').hidden = true;
  $('trafficClientNameForm').hidden = false;
  input.focus();
  input.select();
}

function endRename() {
  $('trafficClientNameForm').hidden = true;
  nameError('');
  if (lastBody) $('trafficClientRename').hidden = lastBody.privacy !== 'full';
}

// Saves the name typed (an empty one, or `clear`, takes the name away), then shows the device and the card again.
async function saveName(clear) {
  if (!shown) return;
  const name = clear ? null : $('trafficClientNameInput').value.trim() || null;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(shown.deviceId)}/clients/${encodeURIComponent(shown.key)}/name`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name })
    });
    if (!res.ok) {
      const detail = await res.json().then((b) => (typeof b.detail === 'string' ? b.detail : null)).catch(() => null);
      return nameError(detail || `Could not save the name (HTTP ${res.status}).`);
    }
    endRename();
    refreshTraffic();
    await load();
  } catch (e) {
    nameError(`Could not save the name: ${e.message}`);
  }
}

function problem(text) {
  $('trafficClientBody').hidden = true;
  const s = $('trafficClientStatus');
  s.hidden = false;
  s.textContent = text;
}

export async function openTrafficClient(deviceId, key) {
  shown = { deviceId, key };
  range = shownRange(); // the card's range
  endRename();
  $('trafficClientRename').hidden = true; // (until the answer says names are shown)
  rangeButtons();
  $('trafficClientTitle').textContent = 'Device traffic';
  $('trafficClientMeta').textContent = '';
  problem('Loading...');
  openModal('trafficClientModal');
  await load();
}

// Escape and Tab; closing it (any way) lets the chart go.
// Escape leaves a rename that is under way (as Cancel), else closes the dialog.
keepKeyboard(modal(), null, {
  onEscape: () => { if (!$('trafficClientNameForm').hidden) { endRename(); $('trafficClientRename').focus(); } else closeModal('trafficClientModal'); },
  onClose: () => { if (chart) chart.unmount(); chart = null; lastBody = null; seq++; endRename(); }
});

// Chart.js keeps the colors it resolved when it drew: a theme change draws the open chart again.
document.addEventListener('themechange', () => { if (lastBody && modal().classList.contains('active')) show(lastBody); });

// The actions this module's markup names (see dispatch.js).
registerActions('submit', { trafficClientNameSave: (el, ev) => { ev.preventDefault(); saveName(false); } });

registerActions('click', {
  openTrafficClient: (el) => openTrafficClient(el.dataset.arg, el.dataset.arg2),
  trafficClientRename: () => startRename(),
  trafficClientRenameCancel: () => { endRename(); $('trafficClientRename').focus(); },
  trafficClientNameClear: () => saveName(true),
  // another range: the figures stay until the new answer replaces them (the buttons show the choice at once)
  trafficClientRange: (el) => {
    const arg = el.dataset.arg;
    if (!shown || !RANGES.some((r) => r.arg === arg) || arg === range) return;
    range = arg;
    rangeButtons();
    load();
  }
});
