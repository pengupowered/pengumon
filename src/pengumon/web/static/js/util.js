// Helpers every module uses: the target name/color table, DOM text and class setters, modals, HTML escaping.

import { registerActions } from './dispatch.js';
import { token } from './chart-theme.js';

// Target order, names and colors come from /api/targets (the probe path, then the DNS probes; `color` per target).
// Both are filled by initTargets() before the first render and mutated in place, so importers keep their reference.
export const canonicalTargetOrder = [];

export function sortTargetsByCanonical(targetIds) {
  return Array.from(targetIds).sort((a, b) => {
    const idxA = canonicalTargetOrder.indexOf(a);
    const idxB = canonicalTargetOrder.indexOf(b);
    return (idxA !== -1 ? idxA : 999) - (idxB !== -1 ? idxB : 999);
  });
}

const targetMeta = {};

export function getTargetInfo(tid) {
  if (targetMeta[tid]) return targetMeta[tid];
  return { name: tid, color: '#58a6ff' };
}

// The color of a target without a configured one: the next categorical slot (tokens --s1..--s8) in path order, so the
// first eight such targets all differ (one color per category made three public servers three identical lines). Past
// the eighth a target takes the first slot no other target of its category has yet, so the members of one group (the
// public servers of the Internet node, a run of ISP hops, the DNS lookups) still differ from each other; a category
// with more than eight is drawn in the neutral ink from the ninth on and told apart by its name. Read from the tokens
// when used (cached until the theme changes), so it follows the theme.
const SLOTS = 8;
const tokenColor = (name) => token(name) || '#7d8a9b'; // cached by token(), which forgets its values on a theme change

export async function initTargets() {
  try {
    const res = await fetch('/api/targets');
    if (res.ok) {
      const targets = await res.json();
      if (Array.isArray(targets)) {
        canonicalTargetOrder.length = 0; // in place: the path's order now (an applied edit can reorder or remove)
        let slot = 0;
        const taken = {}; // category -> the slots its targets have
        targets.forEach(t => {
          const tid = t.target_id || t.id;
          if (!tid) return;
          let token = null;
          if (!t.color) {
            const mine = (taken[t.category] ||= new Set());
            const free = Array.from({ length: SLOTS }, (_, k) => `--s${k + 1}`).find((s) => !mine.has(s));
            token = slot < SLOTS ? `--s${++slot}` : (free || '--ink-3');
            mine.add(token);
          }
          targetMeta[tid] = { name: t.name || tid, category: t.category, address: t.address || '', enabled: t.enabled !== false, group: t.group || '', shortLabel: t.short_label || '',
            get color() { return t.color || tokenColor(token); } };
          if (!canonicalTargetOrder.includes(tid)) canonicalTargetOrder.push(tid);
        });
      }
    }
  } catch (e) {
    console.warn('Failed to load targets config:', e);
  }
}

// An icon from the page's sprite (templates/icons.svg), as markup for innerHTML or as an element.
export const icon = (name, cls = '') =>
  `<svg class="i${cls ? ` ${cls}` : ''}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;

export function iconEl(name, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', `i${cls ? ` ${cls}` : ''}`);
  svg.setAttribute('aria-hidden', 'true');
  const use = svg.appendChild(document.createElementNS(ns, 'use'));
  use.setAttribute('href', `#i-${name}`);
  return svg;
}

// One status vocabulary: a level's icon and word (status is always icon + word + color, never color alone).
export const STATUS = {
  good: { icon: 'ok', word: 'Healthy' },
  warn: { icon: 'warn', word: 'Degraded' },
  serious: { icon: 'warn', word: 'Serious' },
  crit: { icon: 'down', word: 'Down' },
  busy: { icon: 'busy', word: 'Busy' },
  unknown: { icon: 'unknown', word: 'Unknown' }
};

// A status badge as markup: <span class="badge st-LEVEL">icon WORD</span>.
export const badge = (level, word, extra = '') =>
  `<span class="badge st-${level}${extra ? ` ${extra}` : ''}">${icon((STATUS[level] || STATUS.unknown).icon)}${escapeHtml(word ?? (STATUS[level] || STATUS.unknown).word)}</span>`;

// Sets a badge element in place (no markup rebuilt when nothing changed).
export function setBadge(el, level, word) {
  const cls = `${el.classList.contains('quiet') ? 'badge quiet' : 'badge'} st-${level}`;
  const key = `${level}|${word}`;
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  el.className = cls;
  el.replaceChildren(iconEl((STATUS[level] || STATUS.unknown).icon), document.createTextNode(word));
}

// The configured targets in canonical order, as [{id, category, name, address, enabled, color}].
export function targetList() {
  return canonicalTargetOrder.map((id) => ({ id, ...targetMeta[id] }));
}

// A copy of <template id="templateId">'s first element, with its [data-f] descendants by name (instead of global
// ids, so one panel can exist once per device).
export function cloneTemplate(templateId) {
  const root = document.getElementById(templateId).content.firstElementChild.cloneNode(true);
  const refs = {};
  root.querySelectorAll('[data-f]').forEach((el) => { refs[el.dataset.f] = el; });
  return { root, refs };
}

// A device as people know it: its model from the config, else its host, else its id.
export const deviceName = (d) => (d.model ? String(d.model).replace(/_/g, ' ') : (d.host || d.id));

// A clock time (hours and minutes, with seconds on request) in the viewer's locale; with the date for ranges over a day.
// The formatters are made once: toLocaleTimeString with options built a new Intl.DateTimeFormat on every call, and a
// chart relabels hundreds of points on every message.
// The zone Settings names (general.timezone, from /api/info `timezone`), else null: the browser's own. Every time on the
// page is cut on this clock, so a page opened from another zone agrees with the reports and the AI pack. A change (the
// first answer of /api/info, a Save in Settings) is told as a `zonechange` event on the document: the cards that write
// times draw again.
let pageZoneName = null;
let CLOCK = {};
export function setPageZone(name) {
  let zone = name || null;
  if (zone) { try { new Intl.DateTimeFormat([], { timeZone: zone }); } catch { zone = null; } } // a zone this browser cannot name
  if (zone === pageZoneName) return;
  pageZoneName = zone;
  CLOCK = {};
  document.dispatchEvent(new CustomEvent('zonechange'));
}
// Milliseconds -> text in the page's zone, `options` as for Intl.DateTimeFormat (through zonedFormat, below).
export function localFormat(ms, options) {
  const key = JSON.stringify(options);
  return (CLOCK[key] ||= zonedFormat({ zone: pageZoneName }, options))(ms / 1000);
}
// Milliseconds east of UTC the page's clock is at `ms` (the browser's own when Settings names no zone).
export function zoneOffsetMs(ms) {
  if (!pageZoneName) return -new Date(ms).getTimezoneOffset() * 60000;
  const parts = Object.fromEntries((CLOCK.parts ||= new Intl.DateTimeFormat('en-US', {
    timeZone: pageZoneName, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric'
  })).formatToParts(ms).map((p) => [p.type, +p.value]));
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second) - Math.floor(ms / 1000) * 1000;
}
// The next midnight on the page's clock after `ms`, in epoch milliseconds.
export function nextMidnight(ms) {
  if (!pageZoneName) { const d = new Date(ms); d.setHours(24, 0, 0, 0); return d.getTime(); }
  const DAY_MS = 86400000;
  const guess = Math.floor((ms + zoneOffsetMs(ms)) / DAY_MS) * DAY_MS + DAY_MS; // that midnight as wall-clock time read as UTC
  return guess - zoneOffsetMs(guess - zoneOffsetMs(ms)); // minus the offset in force then (a daylight saving day may differ)
}
export function formatClock(ms, withDate = false, seconds = false) {
  const hm = localFormat(ms, seconds ? { hour: '2-digit', minute: '2-digit', second: '2-digit' } : { hour: '2-digit', minute: '2-digit' });
  return withDate ? `${localFormat(ms, { month: 'short', day: 'numeric' })} ${hm}` : hm;
}
// Epoch seconds -> text on the clock a report answer was cut on (data.zone: the zone set in Settings, else this
// browser's), which need not be this browser's. A zone this browser cannot name (a fixed offset, in an older one):
// shifted by data.tz_offset_minutes and read as UTC.
export function zonedFormat(data, opts) {
  try {
    const f = new Intl.DateTimeFormat([], { ...opts, timeZone: data.zone || undefined });
    return (ts) => f.format(ts * 1000);
  } catch {
    const f = new Intl.DateTimeFormat([], { ...opts, timeZone: 'UTC' });
    return (ts) => f.format((ts + (data.tz_offset_minutes || 0) * 60) * 1000);
  }
}

// A round-trip time in ms: a tenth below 100, whole above (`tenths`: always a tenth); `unit` adds " ms". No number: `empty`.
export function formatMs(v, { unit = false, empty = '–', tenths = false } = {}) {
  if (v === null || v === undefined || !Number.isFinite(+v)) return empty;
  const text = v >= 100 && !tenths ? String(Math.round(v)) : (+v).toFixed(1);
  return unit ? `${text} ms` : text;
}

// The middle of the numbers in `values` (null and undefined skipped), the mean of the two middle ones when there is an even
// number (as Python's statistics.median); null for none.
export function median(values) {
  const v = values.filter((x) => x !== null && x !== undefined).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// An id-safe word from free text: lower case, runs of other characters as one underscore, none at the ends.
export const slug = (text) => String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

// The viewer's time zone for the routes that cut days and hours (/api/report, /api/report/loss-grid, /api/uptime,
// /api/ai-pack): `tz`, the browser's IANA zone, which follows daylight saving, and `tz_offset_minutes`, minutes east of UTC
// now, for a browser without one. The server uses the zone set in Settings first. URLSearchParams: `${zoneParams()}` in a
// URL, or copied into another one.
export function zoneParams() {
  const p = new URLSearchParams({ tz_offset_minutes: String(-new Date().getTimezoneOffset()) });
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zone) p.set('tz', zone);
  } catch { /* no Intl time zones: the offset alone */ }
  return p;
}

// A live target's latest probe: its newest round trip, or null when that probe got no answer. Not last_ms, which keeps the
// last answer through an outage and would show a figure for a target that is down.
export const latestMs = (t) => { const r = t.recent_latencies; return r && r.length ? r[r.length - 1] : null; };

// A percentage to `digits` decimals that never rounds to a clean 0 or 100 unless it is exactly that: 99.996 % available is
// not "100.00%", and one lost probe in a million is not "0.000%". Null or undefined: a dash.
export function percentText(v, digits = 2) {
  if (v === null || v === undefined) return '–';
  const text = v.toFixed(digits);
  if (v > 0 && Number(text) === 0) return `<${(10 ** -digits).toFixed(digits)}%`;
  if (v < 100 && Number(text) === 100) return `>${(100 - 10 ** -digits).toFixed(digits)}%`;
  return `${text}%`;
}

// Set the text / the class of an element (none: nothing) only when it changed. `setClass` also serves SVG elements (their className is not a
// string, so the class attribute is set).
export function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

export function setClass(el, cls) {
  if (el && el.getAttribute('class') !== cls) el.setAttribute('class', cls);
}

const openers = new Map(); // modal id -> the element that had focus when it opened, which gets it back on close

// Back closes the topmost dialog (and stays on the page). Opening a dialog gives it a history entry, so Back lands on the
// popstate below; closing it any other way (button, Escape) takes its entry away again, so no Back press is dead. Settings
// and the path editor have their own route and guard (a hash, a draft to ask about), and the first-run question cannot be
// dismissed: none of the three gets an entry here.
const OWN_HISTORY = new Set(['settingsModal', 'pathEditorModal', 'accessSetupModal']);
const pageToken = Math.random().toString(36).slice(2); // an entry of an earlier page load (a reload keeps history.state) is not this page's
const entries = []; // modal ids, one per history entry of ours above the page's own (null: closed out of order, its entry is still there)
let pendingEntries = 0; // entries given up by a close whose history.back() has not run yet
let ownBacks = 0; // history moves we made ourselves: their popstate is not the user's Back
let waiters = []; // functions that want the history settled (afterModalHistory)
let takeTimer = null; // the pending takeEntryAway of a close (canceled when the page starts to unload)
let leaving = false; // the page is unloading (a reload, a link): no more history moves of ours
const ours = () => !!(history.state && history.state.pgModal === pageToken);
const depthNow = () => (ours() ? history.state.depth : 0);

function flushWaiters() {
  if (pendingEntries || ownBacks) return;
  const list = waiters;
  waiters = [];
  list.forEach((fn) => fn());
}

// Runs `fn` once a closing dialog has given its history entry back (a caller that closes a dialog and then changes the
// hash would otherwise push before the entry is taken away, and the later Back would land on the wrong entry).
export function afterModalHistory(fn) {
  if (pendingEntries || ownBacks) waiters.push(fn); else fn();
}

function takeEntryAway() {
  takeTimer = null;
  if (leaving) return;
  const extra = pendingEntries;
  pendingEntries = 0;
  if (extra && depthNow() === entries.length + extra) { // still the entry we gave up (nobody replaced or covered it)
    ownBacks++;
    setTimeout(() => { if (ownBacks) { ownBacks = 0; flushWaiters(); } }, 1000);
    history.go(-extra);
  } else flushWaiters();
}

function addEntry(id) {
  entries.push(id);
  const state = { pgModal: pageToken, depth: entries.length };
  if (pendingEntries === 1 && depthNow() === entries.length) { // a dialog was just closed in this task: its entry is reused
    pendingEntries = 0;
    history.replaceState(state, '');
    flushWaiters();
  } else history.pushState(state, '');
}

function dropEntry(id) {
  const at = entries.lastIndexOf(id);
  if (at < 0) return;
  entries[at] = null;
  if (at !== entries.length - 1) return; // under another dialog: its entry goes when that one's does
  let k = 0;
  while (entries.length && entries[entries.length - 1] === null) { entries.pop(); k++; }
  const first = pendingEntries === 0;
  pendingEntries += k;
  if (first) takeTimer = setTimeout(takeEntryAway, 0); // after what the caller does next in this task (Settings replaces the entry itself)
}

window.addEventListener('popstate', () => {
  if (ownBacks) { ownBacks = 0; flushWaiters(); return; }
  const depth = depthNow();
  while (entries.length > depth) { // the user's Back: close what the entry left behind
    const id = entries.pop();
    if (id && document.getElementById(id)?.classList.contains('active')) closeModal(id);
  }
});
// A history move of ours after a reload or a link has started would cancel that navigation (a traversal aborts it): once the
// page starts to unload, the pending move is dropped (the entry stays: it is this page's, and the next load clears its mark).
window.addEventListener('beforeunload', () => {
  leaving = true;
  if (takeTimer !== null) { clearTimeout(takeTimer); takeTimer = null; }
  // still here after a while: the navigation was canceled (a draft's "leave this page?" answered Stay): carry on
  setTimeout(() => { leaving = false; if (pendingEntries && takeTimer === null) takeTimer = setTimeout(takeEntryAway, 0); }, 2000);
});
let unloading = false; // the page is being unloaded: its fetches are cut off (Firefox rejects them), which is no error
window.addEventListener('pagehide', () => { unloading = true; });
window.addEventListener('pageshow', (ev) => { if (ev.persisted) { leaving = false; unloading = false; } }); // back from the bfcache: the page lives on
// Whether the page is going away (a reload or a link has started): a request that fails now failed because of that.
export const pageLeaving = () => leaving || unloading;
// A reload keeps the entry of a dialog that was open: it is the page now, so it loses the mark (Back stays one press behind).
window.addEventListener('load', () => { if (history.state && history.state.pgModal && history.state.pgModal !== pageToken) history.replaceState(null, ''); }, { once: true });

export function openModal(id) {
  const modal = document.getElementById(id);
  if (!modal.classList.contains('active')) {
    openers.set(id, document.activeElement);
    if (!OWN_HISTORY.has(id)) addEntry(id);
  }
  modal.classList.add('active');
}

export function closeModal(id) {
  const modal = document.getElementById(id);
  modal.classList.remove('active');
  dropEntry(id);
  const back = openers.get(id);
  openers.delete(id);
  const lost = !document.activeElement || document.activeElement === document.body || modal.contains(document.activeElement);
  if (lost && back && back.isConnected && typeof back.focus === 'function') back.focus();
}

// Keyboard for a dialog: focus moves into it when it opens (to the first match of `firstFocus`, else the close button),
// Tab stays inside it and Escape closes it. Call it before openModal; calling it again only updates `firstFocus`.
// `onEscape(ev)` replaces the closing (a dialog that asks first, or has an inner state to leave); `onClose()` runs when the
// dialog goes from open to closed, by any way.
// One listener on the document serves every dialog, so Escape works wherever focus is (a click on plain text leaves it on
// the body) and acts on the topmost open dialog only: the one that opened last (a dossier on top of the incident inspector).
const keyboard = new WeakMap();
const stack = []; // the registered dialogs that are open, the topmost last
function topModal() {
  for (let i = stack.length - 1; i >= 0; i--) if (stack[i].classList.contains('active')) return stack[i];
  return null;
}
let keysBound = false;
function bindKeys() {
  if (keysBound) return;
  keysBound = true;
  document.addEventListener('keydown', (ev) => {
    const modal = topModal();
    if (!modal || ev.defaultPrevented) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      const own = keyboard.get(modal);
      if (own && own.onEscape) own.onEscape(ev); else closeModal(modal.id);
      return;
    }
    if (ev.key !== 'Tab') return;
    const items = [...modal.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]')].filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const inside = modal.contains(document.activeElement);
    if (!inside) { ev.preventDefault(); (ev.shiftKey ? last : first).focus(); }
    else if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });
}
export function keepKeyboard(modal, firstFocus, { onEscape, onClose } = {}) {
  if (keyboard.has(modal)) { keyboard.get(modal).first = firstFocus; return; }
  const state = { first: firstFocus, onEscape, onClose, open: false };
  keyboard.set(modal, state);
  bindKeys();
  const sync = () => {
    const at = stack.indexOf(modal);
    if (modal.classList.contains('active')) {
      state.open = true;
      if (at < 0) stack.push(modal);
      if (!modal.contains(document.activeElement)) {
        ((state.first && modal.querySelector(state.first)) || modal.querySelector('.m-close')).focus();
      }
    } else {
      if (at >= 0) stack.splice(at, 1);
      if (state.open) { state.open = false; if (state.onClose) state.onClose(); }
    }
  };
  new MutationObserver(sync).observe(modal, { attributes: true, attributeFilter: ['class'] });
  sync(); // the dialog may be open already
}

// Why the server would refuse a host (models/hosts.validate_host), in words, or '' when it would take it.
const HOST_RE = /^[A-Za-z0-9]([A-Za-z0-9_-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9_-]*[A-Za-z0-9])?)*\.?$/;
export function hostProblem(v) {
  if (!v) return 'Enter a host name or an IP address.';
  if (/\s/.test(v) || v.startsWith('-')) return 'A host has no spaces and does not start with a dash.';
  // an IPv6 address, a link-local one with its zone (fe80::1%eth0)
  if (v.includes(':')) return /^[0-9A-Fa-f:.]+(%[A-Za-z0-9_.-]+)?$/.test(v) && (v.match(/:/g) || []).length >= 2 ? '' : 'That is not a valid IPv6 address.';
  if (/^[0-9.]+$/.test(v)) return v.split('.').length === 4 && v.split('.').every((p) => p !== '' && Number(p) <= 255 && String(Number(p)) === p) ? '' : 'That is not a valid IPv4 address.';
  return HOST_RE.test(v) && v.length <= 255 ? '' : 'That is not a valid host name.';
}

// A password Pengumon proposes for the sign-in (the first-run question, Settings > Sign-in): 16 characters in four groups,
// from letters and digits that cannot be mistaken for one another (no 0/o, 1/l/i), about 79 bits. crypto.getRandomValues
// works over plain HTTP too (a LAN address is not a secure context).
const PASSWORD_LETTERS = 'abcdefghjkmnpqrstuvwxyz23456789';
export function suggestPassword() {
  const bytes = new Uint8Array(32);
  let out = '';
  while (out.length < 16) {
    crypto.getRandomValues(bytes);
    for (const b of bytes) {
      if (out.length < 16 && b < 248) out += PASSWORD_LETTERS[b % PASSWORD_LETTERS.length]; // 248 = 8 x 31: no bias
    }
  }
  return out.match(/.{4}/g).join('-');
}

// Copies `text` to the clipboard; resolves true when it did. The Clipboard API exists only in a secure context (HTTPS or
// localhost): a page served over plain HTTP on the LAN has no navigator.clipboard, so it copies through a hidden
// textarea and execCommand instead.
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  const back = document.activeElement;
  document.body.appendChild(area);
  area.select();
  let done = false;
  try { done = document.execCommand('copy'); } catch { done = false; }
  area.remove();
  if (back && typeof back.focus === 'function') back.focus();
  return done;
}

// The body of an Export view (Settings, the path editor): the YAML in a textarea, Copy, and a download link. Chrome blocks or
// warns about a file downloaded over plain HTTP from another machine ("insecure download"), whatever the page does;
// Copy works on every connection. `prefix` names the ids (<prefix>ExportText, <prefix>ExportNote) and the Copy action
// (<prefix>ExportCopy), which the dialog registers to call copyExport(prefix).
export function exportViewHtml({ prefix, intro, text, href, file }) {
  return `<h3 class="section-t">Export</h3>
    <p class="fine top">${escapeHtml(intro)}</p>
    <textarea class="input export-text" id="${prefix}ExportText" rows="12" readonly spellcheck="false" aria-label="Exported YAML">${escapeHtml(text)}</textarea>
    <p class="export-b"><button type="button" class="btn sm" data-action="${prefix}ExportCopy"><svg class="i" aria-hidden="true"><use href="#i-copy"/></svg>Copy</button>
      <a class="btn sm" href="${href}" download="${file}"><svg class="i" aria-hidden="true"><use href="#i-download"/></svg>Download file</a>
      <span class="fine" id="${prefix}ExportNote" role="status"></span></p>
    ${window.isSecureContext ? '' : '<p class="fine">This page is served over plain HTTP, so Chrome may block the download as insecure. Copy works on every connection.</p>'}`;
}

// Shows or hides a dialog's Export view (button #<prefix>Export, section #<prefix>ExportView).
export function showExport(prefix, on) {
  const view = document.getElementById(`${prefix}ExportView`);
  document.getElementById(`${prefix}Export`).setAttribute('aria-expanded', String(on));
  view.hidden = !on;
  if (!on) view.innerHTML = '';
}

// The Export button: opens the view with the YAML of `url` (Loading... first), or closes it. `what` names the thing in the
// error ("The path could not be exported (500)"). The view is brought into view and gets the focus, as the dialog's body
// scrolls under its header.
export async function exportToggle(prefix, { url, what, intro, file }) {
  const box = document.getElementById(`${prefix}ExportView`);
  const on = box.hidden;
  showExport(prefix, on);
  if (!on) return;
  const reveal = () => { box.scrollIntoView({ block: 'start' }); box.focus({ preventScroll: true }); };
  box.innerHTML = '<p class="fine">Loading…</p>';
  reveal();
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`The ${what} could not be exported (${res.status}).`);
    box.innerHTML = exportViewHtml({ prefix, intro, text: await res.text(), href: url, file });
  } catch (e) {
    box.innerHTML = `<p class="fine">${escapeHtml(e.message)}</p>`;
  }
  reveal();
}

// Loading something over a draft (an earlier version, a file) throws the draft's edits away: with edits (`dirty`) the dialog
// asks first, through `ask(text, yesLabel, replace)`; without, `replace()` runs at once. Settings and the path editor ask
// in their own way, with the same words.
export function confirmReplaceDraft(dirty, text, replace, ask) {
  if (!dirty) { replace(); return; }
  ask(text, 'Replace the draft', replace);
}

export async function copyExport(prefix) {
  const area = document.getElementById(`${prefix}ExportText`);
  const copied = await copyText(area.value);
  document.getElementById(`${prefix}ExportNote`).textContent = copied ? 'Copied.' : 'Select the text and copy it.';
  if (!copied) area.select();
}

export function escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text).replace(/[&<>"']/g, function(m) {
    return {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    }[m];
  });
}

// ---------- view only (PENGUMON_VIEW_WITHOUT_TOKEN) ----------
// A viewer without the access token may see everything but change nothing: /api/info `can_edit` is false for them, and
// the server refuses their POST/PUT/DELETE/PATCH (and the path export, the device credentials) whatever the page does.
// The page follows it: chrome.js calls setCanEdit() once /api/info answers, which sets <html data-view-only> (CSS dims
// the edit controls) and marks every edit control. An edit control is any element with
// `data-needs-edit` (a button, a link, a form): it gets aria-disabled and the VIEW_ONLY_TIP as its tooltip, also when it
// is drawn later (a MutationObserver, only while view only), and a click on it or a submit of it is stopped before any
// action runs (a capture listener, ahead of dispatch.js; the page then hears `viewonly`, chrome.js says why in a
// toast). Code that acts by itself (an automatic POST) asks canEdit() after `await accessKnown`. Until /api/info answers (or if it fails) the page counts as editable; the server decides.
export const VIEW_ONLY_TIP = 'View only · Sign in to edit';
export const canEdit = () => !document.documentElement.hasAttribute('data-view-only');
let accessResolve;
export const accessKnown = new Promise((resolve) => { accessResolve = resolve; });

// A form is marked through its submit buttons (its other fields stay readable); any other element itself.
const marked = (el) => (el.tagName === 'FORM' ? [...el.querySelectorAll('button[type=submit], input[type=submit]')] : [el]);

function markEditControl(el) {
  if (el.tagName === 'FORM') { marked(el).forEach(markEditControl); return; }
  if (el.dataset.editTitle === undefined) el.dataset.editTitle = el.getAttribute('title') ?? '';
  el.setAttribute('aria-disabled', 'true');
  el.title = VIEW_ONLY_TIP;
}

function unmarkEditControl(el) {
  if (el.tagName === 'FORM') { marked(el).forEach(unmarkEditControl); return; }
  if (el.dataset.editTitle === undefined) return;
  if (el.dataset.editTitle) el.title = el.dataset.editTitle; else el.removeAttribute('title');
  delete el.dataset.editTitle;
  el.removeAttribute('aria-disabled');
}

let editObserver = null;
export function applyEditState(root = document) {
  const view = !canEdit();
  const els = [...(root.matches?.('[data-needs-edit]') ? [root] : []), ...root.querySelectorAll('[data-needs-edit]')];
  els.forEach(view ? markEditControl : unmarkEditControl);
}

function stopEdit(ev) {
  if (canEdit()) return;
  const el = ev.target instanceof Element ? ev.target.closest('[data-needs-edit]') : null;
  if (!el) return;
  ev.preventDefault();
  ev.stopImmediatePropagation();
  document.dispatchEvent(new CustomEvent('viewonly'));
}
document.addEventListener('click', stopEdit, true);
document.addEventListener('submit', stopEdit, true);

export function setCanEdit(on) {
  document.documentElement.toggleAttribute('data-view-only', !on);
  applyEditState();
  if (!on && !editObserver) {
    editObserver = new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => {
      if (n instanceof Element) applyEditState(n);
    })));
    editObserver.observe(document.body, { childList: true, subtree: true });
  } else if (on && editObserver) {
    editObserver.disconnect();
    editObserver = null;
  }
  accessResolve();
  document.dispatchEvent(new CustomEvent('editaccess', { detail: { canEdit: on } }));
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openModal: (el) => openModal(el.dataset.arg),
  closeModal: (el) => closeModal(el.dataset.arg)
});

// A mesh node's backhaul as a few words: "Wired", or the band and signal of its Wi-Fi link ("5 GHz, -54 dBm"); '' when
// the router does not say. The fields are the server's (devices/summary.py mesh_nodes).
export function backhaulText(node) {
  if (node.backhaul === 'wired') return 'Wired';
  if (node.backhaul !== 'wifi') return '';
  const dbm = node.backhaul_rssi_dbm;
  return [node.backhaul_band || 'Wi-Fi', dbm === null || dbm === undefined ? null : `${dbm} dBm`].filter(Boolean).join(', ');
}

// The client `kind` words the server sends (api/clients: phone, computer, ...) as a label and a sprite icon. A kind not
// listed here, or none, is shown as before.
export const CLIENT_KINDS = {
  phone: ['Phone', 'phone'],
  computer: ['Computer', 'host'],
  game: ['Game console', 'host'],
  network: ['Network device', 'router'],
  printer: ['Printer', 'host']
};
