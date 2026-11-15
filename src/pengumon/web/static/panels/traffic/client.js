// The traffic dialog of one device (opened from a row of the Internet use card): its download and upload over 24 hours or
// 7 days (per hour) or 30 days (per day), opening in the range the card shows, with buttons for the others; the totals, the
// busiest hour or day, its apps over the range (as the card counts them: the router's 24-hour counts inside the range, and
// what they do not account for as "Not broken down by app") and the apps of each day, from
// GET /api/devices/{id}/traffic/clients/{key}.
// The pencil by its name (shown where names are: privacy "full") gives it a name in Pengumon, shown instead of the router's
// (PUT /api/devices/{id}/clients/{key}/name); the card is asked again so its row says it too. Back, Escape and Done close it (js/modal.js); every value goes in through textContent.

import { registerActions } from '../../js/dispatch.js';
import { CLIENT_KINDS } from '../../js/device-text.js';
import { formatBytes } from '../../js/format.js';
import { formatClock } from '../../js/clock.js';
import { closeModal, keepKeyboard, openModal } from '../../js/modal.js';
import { NOT_BROKEN_DOWN, NOT_BROKEN_DOWN_WHY, WEEKLY_FROM_HOURS, dayNotes, dayWords, daysOf, gapNotes, hoursOf, loadTraffic, rangeList, refreshTraffic, shownRange, weeklyOf } from './model.js';
import { allStoredWords, fillRangeSelect } from '../../js/ranges.js';
import { hoursChart, legendOf } from './chart.js';
import { appRow } from './view.js';
import { has } from '../../js/values.js';

const $ = (id) => document.getElementById(id);
const modal = () => $('trafficClientModal');

let chart = null;
let seq = 0;
let lastBody = null; // the answer drawn, for a redraw after a theme change
let shown = null; // {deviceId, key} of the device the dialog is open on
let range = '7d'; // the range chosen in the dialog (a range id of js/ranges.js)
const chosen = () => rangeList().find((r) => r.arg === range) || rangeList()[0];

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
  const perDay = !body.series.length && Array.isArray(body.daily); // 30 days and more: a bar per calendar day
  const weekly = perDay && body.hours > WEEKLY_FROM_HOURS; // past 90 days: a bar per week
  const span = body.all ? allStoredWords(Math.round(body.hours / 24), body.since) : chosen().title.toLowerCase(); // "last 7 days"
  const rows = perDay ? (weekly ? weeklyOf(body.daily) : body.daily) : body.series;
  const h = perDay ? daysOf(rows) : hoursOf(body.series, body.hours > 24);
  const busiest = rows.reduce((best, r) => (has(r.down) && (!best || r.down > best.down) ? r : best), null);
  const busy = busiest && busiest.down > 0;
  const stats = $('trafficClientStats');
  stats.replaceChildren(
    stat('Downloaded', formatBytes(body.totals.down), span, 'down'),
    stat('Uploaded', formatBytes(body.totals.up), span, 'up'),
    stat(weekly ? 'Busiest week' : perDay ? 'Busiest day' : 'Busiest hour', busy ? formatBytes(busiest.down) : '–',
      !busy ? 'no traffic' : weekly ? `week of ${dayWords(busiest.day)}` : perDay ? dayWords(busiest.day, true) : `${formatClock(busiest.hour * 1000, true)}-${formatClock((busiest.hour + 3600) * 1000)}`, 'busiest'),
    stat('Mostly', c.traffic_hint || '–', c.traffic_hint ? `at least 80 % of its traffic the router counted apps for, ${span}` : 'no single app stands out', 'hint')
  );
  $('trafficClientPer').textContent = weekly ? 'Per week' : perDay ? 'Per day' : 'Per hour';
  $('trafficClientSpan').textContent = perDay ? `${span}, a bar per ${weekly ? 'week' : 'calendar day'} (the first and last cut by the range)` : span;
  const data = { ...h, label: `${c.label}: download and upload per ${weekly ? 'week' : perDay ? 'day' : 'hour'} over ${body.all ? "" : "the "}${span}, ${formatBytes(body.totals.down)} down, ${formatBytes(body.totals.up)} up.` };
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
  $('trafficClientNotes').replaceChildren(...(perDay ? dayNotes(rows) : gapNotes(h)).map((n) => { const p = make('p', 'trf-note', n.text); p.dataset.v = n.key; return p; }));

  const end = body.apps_window_end;
  const counted = body.apps_covered ? body.apps_covered.hours : 0;
  $('trafficClientAppsWindow').textContent = !counted ? 'no 24 h count of the router in this range'
    : `${counted === body.hours ? span : `counted over ${counted} of ${body.hours} h`}, to ${formatClock(end * 1000, Date.now() / 1000 - end > 20 * 3600)}`;
  const apps = $('trafficClientApps');
  const rest = body.apps_unattributed || { up: 0, down: 0 };
  const left = rest.down > 0 || rest.up > 0;
  const max = Math.max(0, ...body.apps.map((a) => a.down), left ? rest.down : 0);
  const pct = (v) => (max > 0 ? Math.max(2, Math.round((v / max) * 100)) : 0);
  const list = body.apps.map((a) => appRow({ key: a.app, name: a.app, down: formatBytes(a.down), up: formatBytes(a.up), pct: pct(a.down) }));
  if (left) list.push(appRow({ key: 'rest', name: NOT_BROKEN_DOWN, title: NOT_BROKEN_DOWN_WHY, muted: true, down: formatBytes(rest.down), up: formatBytes(rest.up), pct: pct(rest.down) }));
  apps.replaceChildren(...(list.length ? list : [make('p', 'trf-empty', 'The router has no app figures for this device in this range.')]));

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
  }) : [make('li', 'trf-empty', 'No app history by calendar day is stored in this range yet.')]));
  $('trafficClientStatus').hidden = true;
  $('trafficClientBody').hidden = false;
}

// The range control, the chosen one shown.
function rangeButtons() {
  const list = rangeList();
  if (!list.some((r) => r.arg === range)) range = list[0].arg;
  fillRangeSelect($('trafficClientRange'), { options: list.map((r) => ({ id: r.arg, words: r.title })), value: range, action: 'trafficClientRange', label: 'Range' });
}

async function load() {
  const mine = ++seq;
  const { deviceId, key } = shown;
  try {
    let missing = false;
    const body = await loadTraffic(chosen(), async (hours) => {
      const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/traffic/clients/${encodeURIComponent(key)}?hours=${hours}`);
      if (res.status === 404) { missing = true; return null; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }, () => mine === seq);
    if (mine !== seq) return;
    if (missing) return problem('The router has no traffic stored for this device.');
    show(body);
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
  trafficClientNameClear: () => saveName(true)
});
registerActions('change', {
  // another range: the figures stay until the new answer replaces them (the control shows the choice at once)
  trafficClientRange: (el) => {
    const arg = el.value;
    if (!shown || !rangeList().some((r) => r.arg === arg) || arg === range) return;
    range = arg;
    rangeButtons();
    load();
  }
});
