// Dialogs: opening and closing, the history entries that make Back close them, and their keyboard.

import { registerActions } from './dispatch.js';

const openers = new Map(); // modal id -> the element that had focus when it opened, which gets it back on close

// ---------- Back and the dialogs ----------
// One scheme for every dialog (Settings and the path editor too), and the page never moves the history by itself when a
// dialog closes: no history.go(), no timer.
//  - Opening a dialog pushes one entry (state `pgModal`). If the page is on a spent entry (below) the dialog takes that one
//    over instead, so a session does not pile up entries. A dialog that a link opens (#settings) takes the entry the link
//    made over (`pgAdopted`).
//  - Closing it by its own controls (button, Escape, Save) only marks its entry spent (replaceState to the page's own
//    state and URL again, with `pgSpent`). Nothing is traversed, so a hash set, a push or a reload in the same task as the
//    close cannot be undone or land too far back.
//  - Back on an open dialog closes it, after the dialog's hook (onModalBack) had its say: a dialog with unsaved changes asks
//    and stays, and its entry is put back.
//  - A spent entry is a copy of the page: a Back that lands on one, or starts from one, goes on past it until it reaches
//    the page it stands for, so no Back press is dead. A Forward onto one only lands: nothing opens again.
// The first-run question cannot be dismissed and gets no entry. The Navigation API (Chromium, Firefox) tells a traversal
// from a push (a hash set fires popstate too) and Back from Forward; a browser without it treats every popstate as a Back.
const NO_ENTRY = new Set(['accessSetupModal']);
const pageToken = Math.random().toString(36).slice(2); // an entry of an earlier page load (a reload keeps history.state) is not this page's
const entries = []; // the open dialogs that have an entry, in the order opened: {id, serial, state, url}
const backHooks = new Map(); // modal id -> () => void, what Back does instead of closing it (see onModalBack)
let serial = 0;
let current = history.state; // the state of the entry the page is on
let landing = null; // the last navigation, from currententrychange: {type, from, dir}
let skip = null; // {past} while a Back of ours is moving over spent entries
let leaving = false; // the page is unloading (a reload, a link): see pageLeaving
const plain = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const ours = (st) => plain(st) && st.pgModal === pageToken;
// Whether an entry stands for nothing: a spent one, or one whose dialog is closed (it was closed under another dialog).
const dead = (st) => plain(st) && (st.pgSpent !== undefined || (st.pgModal !== undefined && !(ours(st) && entries.some((e) => e.serial === st.pgSerial))));
// The page's own part of an entry's state (what the code that set it asked for), without ours.
function pageState(st) {
  if (!plain(st)) return null;
  if (st.pgModal !== undefined) return st.pgBelow ?? null;
  const rest = Object.fromEntries(Object.entries(st).filter(([k]) => !k.startsWith('pg')));
  return Object.keys(rest).length ? rest : null;
}
// A reload keeps the entry of a dialog that was open: the dialog is closed now and the entry is the page.
if (plain(history.state) && history.state.pgModal !== undefined && history.state.pgModal !== pageToken) history.replaceState(pageState(history.state), '');
current = history.state;

if (window.navigation) {
  navigation.addEventListener('currententrychange', (ev) => {
    const from = current;
    current = history.state;
    landing = { type: ev.navigationType, from, dir: ev.navigationType === 'traverse' ? Math.sign(navigation.currentEntry.index - ev.from.index) : 0 };
  });
}

function addEntry(id, { hash = '', adopt = false } = {}) {
  const here = history.state;
  const page = `${location.pathname}${location.search}`;
  const reuse = dead(here);
  // a dialog with an address of its own (#settings) has it while open and the page's address without a hash after it
  const entry = { id, serial: ++serial, url: hash ? `${page}${hash}` : undefined };
  entry.state = { pgModal: pageToken, pgSerial: entry.serial, pgId: id, pgBelow: pageState(here), pgUrl: hash ? page : `${page}${location.hash}` };
  if (adopt && !reuse) entry.state.pgAdopted = true;
  entries.push(entry);
  if (reuse || adopt) history.replaceState(entry.state, '', entry.url); else history.pushState(entry.state, '', entry.url);
  current = history.state;
}

// The dialog closed: when the page is on its entry, that entry is marked spent (or, for one a link made, becomes the plain
// page again). Otherwise (it closed under another dialog, or Back already left its entry) the entry stays as it is, and is
// dead from now on because the dialog is no longer in `entries`.
function retire(id) {
  const at = entries.findLastIndex((e) => e.id === id);
  if (at < 0) return;
  const [entry] = entries.splice(at, 1);
  const st = history.state;
  if (!ours(st) || st.pgSerial !== entry.serial) return;
  const below = st.pgBelow;
  history.replaceState(st.pgAdopted ? below : { ...below, pgSpent: pageToken }, '', st.pgUrl);
  current = history.state;
}

// What Back does for a dialog that is open and whose entry it left: `fn` runs instead of the plain closing. It either closes
// the dialog itself (through closeModal) or leaves it open (a question about unsaved changes); an open dialog gets its entry back.
export function onModalBack(id, fn) {
  backHooks.set(id, fn);
}

// Whether the page is on an entry that stands for a closed dialog (a Forward onto one must not open it again).
export const entryIsSpent = () => dead(history.state);

function closeAbove(now) {
  const level = ours(now) ? now.pgSerial : 0;
  while (entries.length && entries[entries.length - 1].serial > level) {
    const top = entries[entries.length - 1];
    const hook = backHooks.get(top.id);
    if (hook) hook(); else closeModal(top.id);
    if (entries.includes(top)) { // it stays open: its entry goes back where Back took it from
      history.pushState(top.state, '', top.url);
      current = history.state;
      return;
    }
  }
}

window.addEventListener('popstate', () => {
  const nav = landing;
  landing = null;
  const from = nav ? nav.from : current;
  current = history.state;
  if (nav && (nav.type !== 'traverse' || nav.dir > 0)) { skip = null; return; } // a push (a hash set), or a Forward
  const now = history.state;
  let past = skip ? skip.past : dead(from); // from a spent entry: one live entry on, the page it stands for, has to be passed too
  skip = null;
  let more = dead(now);
  if (!more && past) { past = false; more = true; }
  if (more && (!window.navigation || navigation.canGoBack)) {
    skip = { past };
    history.back();
    return;
  }
  closeAbove(now);
});
// The page is going away (a reload, a link): see pageLeaving. A canceled navigation (a draft's "leave this page?" answered
// Stay) leaves the page here, so the flag is dropped again after a while.
window.addEventListener('beforeunload', () => {
  leaving = true;
  setTimeout(() => { leaving = false; }, 2000);
});
let unloading = false; // the page is being unloaded: its fetches are cut off (Firefox rejects them), which is no error
window.addEventListener('pagehide', () => { unloading = true; });
window.addEventListener('pageshow', (ev) => { if (ev.persisted) { leaving = false; unloading = false; } }); // back from the bfcache: the page lives on
// Whether the page is going away (a reload or a link has started): a request that fails now failed because of that.
export const pageLeaving = () => leaving || unloading;

// `hash`: the address the dialog has while open ('#settings'). `adopt`: a link to that address opened it, so the entry the
// link made is the dialog's.
export function openModal(id, opts) {
  const modal = document.getElementById(id);
  if (!modal.classList.contains('active')) {
    openers.set(id, document.activeElement);
    if (!NO_ENTRY.has(id)) addEntry(id, opts);
  }
  modal.classList.add('active');
}
export function closeModal(id) {
  const modal = document.getElementById(id);
  modal.classList.remove('active');
  retire(id);
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
// The controls Tab visits in a dialog. A group of radio buttons is one stop, as in the browser: the checked one, or the first
// when none is checked (the trap would otherwise take an unchecked first button for the start, and Shift+Tab from the real
// one would leave the dialog).
function tabStops(modal) {
  const items = [...modal.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]')].filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
  const group = (e) => (e.type === 'radio' && e.name ? `${e.form ? [...document.forms].indexOf(e.form) : ''}/${e.name}` : '');
  const keep = new Map(); // radio group -> the button of it that is the stop
  for (const e of items) {
    const key = group(e);
    if (key && (!keep.has(key) || (e.checked && !keep.get(key).checked))) keep.set(key, e);
  }
  return items.filter((e) => !group(e) || keep.get(group(e)) === e);
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
    const items = tabStops(modal);
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

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openModal: (el) => openModal(el.dataset.arg),
  closeModal: (el) => closeModal(el.dataset.arg)
});
