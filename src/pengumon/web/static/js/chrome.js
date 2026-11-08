// Page chrome: the header's pop-up menus (the theme menu among them), the LAN address, View only and Sign out.
//
// Theme: themes.js (the theme, light or dark, the effect, the cards' colors); the header's theme button opens its menu.
// Charts are drawn on canvas and cannot read CSS variables, so Chart.js's defaults are set from the tokens
// (chart-theme.js) and every chart is redrawn when the theme changes; modules give their charts no colors of their own
// except series colors.
//
// Sign out: the header button shows only for a browser signed in with the access token (/api/info `session`).
// View only: a viewer without the token (/api/info `can_edit` false) sees "View only · Sign in to edit" in the header,
// and every edit control is disabled with that tooltip (util.js setCanEdit).
//
// Menus: a button with data-action="toggleMenu" data-arg="<menu id>" opens that .menu under itself; a click
// elsewhere, Escape or a choice closes it. The phone header's "more" menu is a copy of the header's tool buttons.

import { registerActions } from './dispatch.js';
import { state } from './state.js';
import { fillThemeMenu, initThemes } from './themes.js';
import { VIEW_ONLY_TIP, copyText, setCanEdit, setPageZone } from './util.js';


// ---------- the LAN address chip (/api/info) ----------
let lanUrl = '';

// Also after the sign-in changed (the first-run question, Settings > Sign-in): Sign out and View only follow it.
export function fetchNetworkInfo() {
  fetch('/api/info', { cache: 'no-store' })
    .then(r => r.json())
    .then(info => {
      if (info.primary_lan_url) {
        lanUrl = info.primary_lan_url;
        const pill = document.getElementById('lanPill');
        const link = document.getElementById('lanUrlLink');
        link.href = lanUrl;
        link.textContent = lanUrl.replace(/^https?:\/\//, ''); // the scheme is no help to read, and costs the header 45 px
        link.title = lanUrl;
        pill.hidden = false;
        fitHeader();
      }
      showSignOut(info.session);
      showAccess(info.can_edit !== false); // an older server without the field: editable, as before
      // for any module that needs it (one request per page load): state.networkInfo, then a `networkinfo` event
      setPageZone(info.timezone); // Settings' zone: every time on the page follows it
      state.networkInfo = info;
      document.dispatchEvent(new CustomEvent('networkinfo', { detail: info }));
    })
    .catch((e) => { setCanEdit(true); console.error(e); }); // unknown: the server still decides each request
}

// The LAN address never shrinks to a cut-off "LAN": where the header has no room for it (a long status pill, the View only
// chip), the chip leaves the header and the address is the first item of the "more" menu (the topbar's attribute `data-lan-menu`
// shows that button on a wide screen too, and style.css hides the tool copies there). Measured with the chip in place,
// so the attribute is taken off first; the browser paints only the end result.
function fitHeader() {
  const bar = document.getElementById('topbar');
  const link = document.getElementById('lanUrlLink');
  const text = document.getElementById('statusPillText');
  if (!bar || !link || !text) return;
  delete bar.dataset.lanMenu;
  if (!lanUrl || window.innerWidth <= 720) return; // the phone's menu lists the address by itself (lanMenuItem)
  if (window.innerWidth <= 1180) { bar.dataset.lanMenu = ''; return; } // style.css hides the chip there: the menu has it
  const cut = (el) => el.scrollWidth > el.getBoundingClientRect().width; // an ellipsis shows for even a fraction of a pixel
  if (cut(link)) { bar.dataset.lanMenu = ''; return; }
  if (!cut(text)) return;
  bar.dataset.lanMenu = ''; // the status title is cut: does it fit without the address?
  if (cut(text)) delete bar.dataset.lanMenu;
}

// A row in the "more" menu with the address and a copy action, for when the header's chip is not shown.
function lanMenuItem(menu) {
  let item = menu.querySelector('.menu-lan');
  if (!item) {
    item = document.createElement('button');
    item.type = 'button';
    item.className = 'menu-lan';
    item.setAttribute('role', 'menuitem');
    item.dataset.action = 'copyLanUrl';
    menu.prepend(item);
  }
  const chip = document.getElementById('lanPill');
  item.hidden = !lanUrl || getComputedStyle(chip).display !== 'none';
  item.textContent = `LAN address ${lanUrl} (copy)`;
  item.title = 'Copy the LAN address';
}

function watchHeader() {
  let queued = false;
  const queue = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; fitHeader(); }); } };
  new ResizeObserver(queue).observe(document.querySelector('.tb-inner'));
  // the status pill's words and the View only / Sign out chips change what is left for the address
  new MutationObserver(queue).observe(document.getElementById('topbar'), { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['hidden'] });
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

async function copyLanUrl() {
  if (!lanUrl) return;
  if (!(await copyText(lanUrl))) { prompt('Copy LAN URL:', lanUrl); return; }
  const use = document.querySelector('#copyIcon use');
  use.setAttribute('href', '#i-ok');
  document.getElementById('btnCopyLan').dataset.copied = '1';
  setTimeout(() => { use.setAttribute('href', '#i-copy'); delete document.getElementById('btnCopyLan').dataset.copied; }, 2000);
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

// Opens `menu` (a .menu.fixed element) under `button` (or under `anchor`); a second press on the same button closes it.
export function showMenu(menu, button, anchor = button) {
  const same = openMenu && openMenu.menu === menu && openMenu.button === button;
  closeMenus();
  if (same) return;
  menu.hidden = false;
  placeMenu(menu, anchor);
  button.setAttribute('aria-expanded', 'true');
  openMenu = { menu, button };
  menu.querySelector('a, button:not([disabled])')?.focus();
}

export function toggleMenu(button) {
  const menu = document.getElementById(button.dataset.arg);
  if (!menu) return;
  if (menu.id === 'headerMenu') { fillHeaderMenu(menu); lanMenuItem(menu); }
  if (menu.id === 'themeMenu') fillThemeMenu(menu);
  // a button in the phone's "more" menu: that menu closes, so the new one opens under the "more" button
  const anchor = button.closest('.menu') ? document.querySelector('.tb-more') || button : button;
  showMenu(menu, button, anchor);
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
  watchHeader();
  initRovingCells();
  fetchNetworkInfo();
  initThemes();
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
