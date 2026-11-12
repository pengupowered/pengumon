// Mobile tab bar. At 720 px and below the dashboard shows one group of cards at a time, chosen by the bottom bar and
// kept in location.hash (#live, #network, ...) so the back button and a reload return to it. The tabs are the buttons
// of the bar; every panel section names its tab in data-tab (set by layout.js from the panel's registry entry).
// Wider than that there is no bar and every panel that is shown appears. A tab exists while at least one of its
// panels is available on this installation and not hidden in the layout.
//
// One rule decides whether a panel section shows: it is available, the layout does not hide it, and (on a narrow
// screen) its tab is the current one. layout.js keeps the first two in data-available and data-layout-hidden.

import { registerActions } from './dispatch.js';

const DEFAULT_TAB = 'live';
const NARROW = '(max-width: 720px)';

const narrow = () => window.matchMedia(NARROW).matches;
const panelSections = () => [...document.querySelectorAll('[data-panel]')];
const tabsOf = (el) => (el.dataset.tab || '').split(/\s+/).filter(Boolean);
const wanted = (el) => el.dataset.available !== '0' && el.dataset.layoutHidden !== '1';
const barButtons = () => [...document.querySelectorAll('#mobileTabs [data-arg]')];

// The tabs that have a panel to show.
function presentTabs() {
  return new Set(panelSections().filter(wanted).flatMap(tabsOf));
}

function currentTab(present) {
  const raw = location.hash.replace(/^#/, '');
  let name = raw;
  try { name = decodeURIComponent(raw); } catch (e) { /* a lone % in the address: use it as it is, it matches no tab */ }
  if (present.has(name)) return name;
  if (present.has(DEFAULT_TAB)) return DEFAULT_TAB;
  const first = barButtons().map((b) => b.dataset.arg).find((t) => present.has(t));
  return first || DEFAULT_TAB;
}

export function applyTabs() {
  const on = narrow();
  const present = presentTabs();
  const current = currentTab(present);
  panelSections().forEach((el) => {
    el.hidden = !(wanted(el) && (!on || tabsOf(el).includes(current)));
  });
  barButtons().forEach((b) => {
    const name = b.dataset.arg;
    b.hidden = !present.has(name);
    const selected = name === current;
    b.setAttribute('aria-selected', String(selected));
    b.tabIndex = selected ? 0 : -1;
  });
}

// A dot on a tab whose cards are not fine: the worst status among the cards of that tab (data-status of each
// .panel),. Healthy and unknown show nothing; the dot is also spelled out in the
// button's accessible name (status is never color alone).
const RANKS = { busy: 1, warn: 2, crit: 3 };
const WORDS = { busy: 'busy, expected', warn: 'needs attention', crit: 'problem' };

function paintTabDots() {
  const worst = {};
  const note = (tab, level) => { if ((RANKS[level] || 0) > (RANKS[worst[tab]] || 0)) worst[tab] = level; };
  panelSections().filter((el) => el.dataset.available !== '0' && el.dataset.layoutHidden !== '1').forEach((el) => {
    el.querySelectorAll('.panel[data-status]').forEach((card) => tabsOf(el).forEach((t) => note(t, card.dataset.status)));
  });
  barButtons().forEach((b) => {
    const level = worst[b.dataset.arg];
    let dot = b.querySelector('.tab-dot');
    if (!level) {
      dot?.remove();
      b.removeAttribute('aria-label');
      return;
    }
    if (!dot) {
      dot = document.createElement('i');
      dot.setAttribute('aria-hidden', 'true');
      b.appendChild(dot);
    }
    dot.className = `tab-dot st-${level}`;
    b.setAttribute('aria-label', `${b.textContent.trim()}: ${WORDS[level]}`);
  });
}

// A tab press changes the hash (one history entry per tab); hashchange does the rest.
export function selectTab(name) {
  if (!presentTabs().has(name)) return;
  if (location.hash === `#${name}`) return;
  location.hash = name;
  window.scrollTo(0, 0);
}

registerActions('click', { selectTab: (el) => selectTab(el.dataset.arg) });

export function initTabs() {
  window.addEventListener('hashchange', applyTabs);
  window.matchMedia(NARROW).addEventListener('change', applyTabs);
  applyTabs();
  paintTabDots();
  let pending = 0;
  const soon = () => { if (!pending) pending = requestAnimationFrame(() => { pending = 0; paintTabDots(); }); };
  new MutationObserver(soon).observe(document.getElementById('panels'), { subtree: true, attributes: true, attributeFilter: ['data-status', 'data-layout-hidden', 'data-available'] });
}
