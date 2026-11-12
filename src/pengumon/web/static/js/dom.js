// Small DOM helpers: sprite icons, status badges, cloned templates, text and class setters that skip no-op writes.

import { escapeHtml } from './escape.js';

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

// ids, so one panel can exist once per device).
export function cloneTemplate(templateId) {
  const root = document.getElementById(templateId).content.firstElementChild.cloneNode(true);
  const refs = {};
  root.querySelectorAll('[data-f]').forEach((el) => { refs[el.dataset.f] = el; });
  return { root, refs };
}

// string, so the class attribute is set).
export function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

export function setClass(el, cls) {
  if (el && el.getAttribute('class') !== cls) el.setAttribute('class', cls);
}

// A box that is drawn again only when `key` (what it shows) changed; hidden when `build` draws nothing.
export function paint(box, key, build) {
  const sig = JSON.stringify(key);
  if (box._sig === sig) return;
  box._sig = sig;
  box.replaceChildren();
  build(box);
  box.hidden = !box.childNodes.length;
}
