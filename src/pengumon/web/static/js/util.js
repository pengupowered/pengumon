// Helpers every module uses: the target name/colour table, DOM text and class setters, modals, HTML escaping.

import { registerActions } from './dispatch.js';

// Target order, names and colours come from /api/targets (config.yaml: targets, then DNS probes; `color` per target).
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
// when used, so it follows the theme.
const SLOTS = 8;
const tokenColor = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#7d8a9b';

export async function initTargets() {
  try {
    const res = await fetch('/api/targets');
    if (res.ok) {
      const targets = await res.json();
      if (Array.isArray(targets)) {
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
          targetMeta[tid] = { name: t.name || tid, category: t.category, address: t.address || '', enabled: t.enabled !== false,
            get color() { return t.color || tokenColor(token); } };
          if (!canonicalTargetOrder.includes(tid)) {
            canonicalTargetOrder.push(tid);
          }
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

export function openModal(id) {
  document.getElementById(id).classList.add('active');
}

export function closeModal(id) {
  document.getElementById(id).classList.remove('active');
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
