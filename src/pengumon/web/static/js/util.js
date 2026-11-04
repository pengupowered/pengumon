// Helpers every module uses: the target name/colour table, DOM text and class setters, modals, HTML escaping.

import { registerActions } from './dispatch.js';
import { token } from './chart-theme.js';

// Target order, names and colours come from /api/targets (the probe path, then the DNS probes; `color` per target).
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

// The colour of a target without a configured one: the next categorical slot (tokens --s1..--s8) in path order, so the
// first eight such targets all differ (one colour per category made three public servers three identical lines). Past
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
          targetMeta[tid] = { name: t.name || tid, category: t.category, address: t.address || '', enabled: t.enabled !== false, group: t.group || '',
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

// One status vocabulary: a level's icon and word (status is always icon + word + colour, never colour alone).
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

// A clock time for chart axes; with the date for ranges over a day.
export function formatClock(ms, withDate) {
  const d = new Date(ms);
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return withDate ? `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${hm}` : hm;
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

export function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

export function setClass(el, cls) {
  if (el.className !== cls) el.className = cls;
}

const openers = new Map(); // modal id -> the element that had focus when it opened, which gets it back on close

export function openModal(id) {
  const modal = document.getElementById(id);
  if (!modal.classList.contains('active')) openers.set(id, document.activeElement);
  modal.classList.add('active');
}

export function closeModal(id) {
  const modal = document.getElementById(id);
  modal.classList.remove('active');
  const back = openers.get(id);
  openers.delete(id);
  const lost = !document.activeElement || document.activeElement === document.body || modal.contains(document.activeElement);
  if (lost && back && back.isConnected && typeof back.focus === 'function') back.focus();
}

// Keyboard for a dialog: focus moves into it when it opens (to the first match of `firstFocus`, else the close button),
// Tab stays inside it and Escape closes it. Call it before openModal; calling it again only updates `firstFocus`.
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
      closeModal(modal.id);
      return;
    }
    if (ev.key !== 'Tab') return;
    const items = [...modal.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')].filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const inside = modal.contains(document.activeElement);
    if (!inside) { ev.preventDefault(); (ev.shiftKey ? last : first).focus(); }
    else if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });
}
export function keepKeyboard(modal, firstFocus) {
  if (keyboard.has(modal)) { keyboard.get(modal).first = firstFocus; return; }
  const state = { first: firstFocus };
  keyboard.set(modal, state);
  bindKeys();
  const sync = () => {
    const at = stack.indexOf(modal);
    if (modal.classList.contains('active')) {
      if (at < 0) stack.push(modal);
      if (!modal.contains(document.activeElement)) {
        ((state.first && modal.querySelector(state.first)) || modal.querySelector('.m-close')).focus();
      }
    } else if (at >= 0) stack.splice(at, 1);
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
