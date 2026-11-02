// Page chrome: the light/dark theme, the chart colours that follow it, and the header's pop-up menus.
//
// Theme: the operating system's choice applies until the person picks one with the header button; the pick is kept
// in this browser (localStorage `pengumon.theme`) and set as <html data-theme>, which tokens.css reads. Charts are
// drawn on canvas and cannot read CSS variables, so Chart.js's defaults are set from the tokens here and every chart
// is redrawn when the theme changes; modules give their charts no colours of their own except series colours.
//
// Sign out: the header button shows only for a browser signed in with the access token (/api/info `session`).
// View only: a viewer without the token (/api/info `can_edit` false) sees "View only · Sign in to edit" in the header,
// and every edit control is disabled with that tooltip (util.js setCanEdit).
//
// Menus: a button with data-action="toggleMenu" data-arg="<menu id>" opens that .menu under itself; a click
// elsewhere, Escape or a choice closes it. The phone header's "more" menu is a copy of the header's tool buttons.

import { registerActions } from './dispatch.js';
import { state } from './state.js';
import { applyChartTheme, token } from './chart-theme.js';
import { VIEW_ONLY_TIP, setCanEdit } from './util.js';

const THEME_KEY = 'pengumon.theme';
const dark = window.matchMedia('(prefers-color-scheme: dark)');


function storedTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === 'light' || t === 'dark' ? t : '';
  } catch (e) {
    return '';
  }
}

export const isDark = () => (document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : dark.matches);

function themeButton() {
  const button = document.getElementById('btnTheme');
  if (!button) return;
  const next = isDark() ? 'light' : 'dark';
  button.setAttribute('aria-label', `Switch to the ${next} theme`);
  button.title = `Switch to the ${next} theme`;
  button.querySelector('use')?.setAttribute('href', isDark() ? '#i-sun' : '#i-moon');
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = token('--bg');
}

function setTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  themeButton();
  applyChartTheme();
  document.dispatchEvent(new CustomEvent('themechange'));
}

export function toggleTheme() {
  const next = isDark() ? 'light' : 'dark';
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (e) {
    // not kept; applies until the page is closed
  }
  setTheme(next);
}

// ---------- the LAN address chip (/api/info) ----------
let lanUrl = '';

function fetchNetworkInfo() {
  fetch('/api/info')
    .then(r => r.json())
    .then(info => {
      if (info.primary_lan_url) {
        lanUrl = info.primary_lan_url;
        const pill = document.getElementById('lanPill');
        const link = document.getElementById('lanUrlLink');
        link.href = lanUrl;
        link.textContent = lanUrl;
        pill.hidden = false;
      }
      showSignOut(info.session);
      showAccess(info.can_edit !== false); // an older server without the field: editable, as before
      // for any module that needs it (one request per page load): state.networkInfo, then a `networkinfo` event
      state.networkInfo = info;
      document.dispatchEvent(new CustomEvent('networkinfo', { detail: info }));
    })
    .catch((e) => { setCanEdit(true); console.error(e); }); // unknown: the server still decides each request
}

// ---------- view only (util.js canEdit) ----------
function showAccess(editable) {
  setCanEdit(editable);
  const chip = document.getElementById('viewOnly');
  if (chip) chip.hidden = editable;
}

// ---------- sign out (dialogs/sign-out) ----------
// /api/info says whether this browser signed in with the access token's cookie (homeguard's session_status): only then
// is there a sign-in to end. Every [data-sign-out] follows it, the phone menu's copy of the header button included.
function showSignOut(session) {
  document.querySelectorAll('[data-sign-out]').forEach((el) => { el.hidden = !session?.sign_out; });
  document.querySelectorAll('[data-sign-out-everywhere]').forEach((el) => { el.hidden = !session?.sign_out_everywhere; });
}

function copyLanUrl() {
  if (!lanUrl) return;
  navigator.clipboard.writeText(lanUrl).then(() => {
    const use = document.querySelector('#copyIcon use');
    use.setAttribute('href', '#i-ok');
    document.getElementById('btnCopyLan').dataset.copied = '1';
    setTimeout(() => { use.setAttribute('href', '#i-copy'); delete document.getElementById('btnCopyLan').dataset.copied; }, 2000);
  }).catch(() => {
    prompt('Copy LAN URL:', lanUrl);
  });
}

// ---------- menus ----------
let openMenu = null; // { menu, button }

export function closeMenus() {
  if (!openMenu) return;
  openMenu.menu.hidden = true;
  openMenu.button.setAttribute('aria-expanded', 'false');
  openMenu = null;
}

function fillHeaderMenu(menu) {
  if (menu.childElementCount) return;
  const tools = document.getElementById('headerTools');
  [...tools.querySelectorAll(':scope > .btn, :scope > div .menu a')].forEach((el) => {
    const item = el.cloneNode(true);
    item.removeAttribute('id');
    item.setAttribute('role', 'menuitem');
    item.classList.remove('btn', 'icon');
    if (!item.querySelector('.lbl') && !item.textContent.trim()) item.append(el.title || el.getAttribute('aria-label') || '');
    menu.appendChild(item);
  });
}

function placeMenu(menu, button) {
  const b = button.getBoundingClientRect();
  menu.style.top = `${b.bottom + 6}px`;
  menu.style.left = `${Math.max(8, Math.min(b.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8))}px`;
}

// Opens `menu` (a .menu.fixed element) under `button`; a second press on the same button closes it.
export function showMenu(menu, button) {
  const same = openMenu && openMenu.menu === menu && openMenu.button === button;
  closeMenus();
  if (same) return;
  menu.hidden = false;
  placeMenu(menu, button);
  button.setAttribute('aria-expanded', 'true');
  openMenu = { menu, button };
  menu.querySelector('a, button:not([disabled])')?.focus();
}

export function toggleMenu(button) {
  const menu = document.getElementById(button.dataset.arg);
  if (!menu) return;
  if (menu.id === 'headerMenu') fillHeaderMenu(menu);
  showMenu(menu, button);
}

// A short confirmation at the bottom of the screen.
let toastTimer = 0;
export function toast(message) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'toast';
    el.setAttribute('role', 'status');
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), 2400);
}

registerActions('click', {
  copyLanUrl: () => copyLanUrl(),
  toggleTheme: () => toggleTheme(),
  toggleMenu: (el) => toggleMenu(el)
});

// One tooltip for every small cell that carries data-tip="Title · detail · detail".
function initCellTips() {
  const tip = document.createElement('div');
  tip.className = 'tip';
  tip.style.position = 'fixed';
  tip.setAttribute('role', 'presentation');
  document.body.appendChild(tip);
  document.addEventListener('pointerover', (ev) => {
    const cell = ev.target instanceof Element ? ev.target.closest('[data-tip]') : null;
    if (!cell) return;
    const [title, ...rest] = cell.dataset.tip.split(' · ');
    tip.replaceChildren();
    const t = tip.appendChild(document.createElement('div'));
    t.className = 't';
    t.textContent = title;
    rest.forEach((text) => {
      const r = tip.appendChild(document.createElement('div'));
      r.className = 'r';
      r.textContent = text;
    });
    tip.classList.add('on');
    const b = cell.getBoundingClientRect();
    const w = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(8, b.left + b.width / 2 - w / 2), window.innerWidth - w - 8)}px`;
    tip.style.top = `${b.top - tip.offsetHeight - 8 < 8 ? b.bottom + 8 : b.top - tip.offsetHeight - 8}px`;
  });
  document.addEventListener('pointerout', (ev) => {
    if (ev.target instanceof Element && ev.target.closest('[data-tip]')) tip.classList.remove('on');
  });
  // a cell whose click scrolls the page would leave the tip floating over something else
  const hide = () => tip.classList.remove('on');
  window.addEventListener('scroll', hide, { passive: true });
  document.addEventListener('click', hide);
}

// A grid of time cells that are all buttons ([data-roving] on their parent): one Tab stop for the whole grid, and the arrow
// keys, Home and End move between its buttons, so a keyboard reaches the next panel in one Tab rather than after every
// cell. The renderer gives the first button tabindex 0 and the rest -1; data-roving="24" makes Up and Down move by 24.
function initRovingCells() {
  document.addEventListener('keydown', (ev) => {
    const cell = ev.target instanceof Element ? ev.target.closest('[data-roving] > button') : null;
    if (!cell) return;
    const group = cell.parentElement;
    const items = [...group.children].filter((e) => e.tagName === 'BUTTON');
    const i = items.indexOf(cell);
    const cols = Number(group.dataset.roving) || 0;
    const step = { ArrowRight: 1, ArrowLeft: -1, Home: -i, End: items.length - 1 - i, ...(cols ? { ArrowDown: cols, ArrowUp: -cols } : {}) }[ev.key];
    if (step === undefined) return;
    ev.preventDefault();
    const next = items[Math.min(items.length - 1, Math.max(0, i + step))];
    cell.tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  });
}

export function initChrome() {
  document.addEventListener('viewonly', () => toast(VIEW_ONLY_TIP));
  initCellTips();
  initRovingCells();
  fetchNetworkInfo();
  setTheme(storedTheme());
  dark.addEventListener('change', () => { if (!document.documentElement.dataset.theme) setTheme(''); });
  document.addEventListener('click', (ev) => {
    if (!openMenu) return;
    const t = ev.target instanceof Element ? ev.target : null;
    if (t && openMenu.button.contains(t)) return;
    if (t && openMenu.menu.contains(t) && !t.closest('a, button')) return;
    closeMenus();
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape' || !openMenu) return;
    const button = openMenu.button;
    closeMenus();
    button.focus();
  });
  window.addEventListener('resize', closeMenus);
}

// The theme as early as possible (this module loads before the page is shown).
if (storedTheme()) document.documentElement.dataset.theme = storedTheme();
