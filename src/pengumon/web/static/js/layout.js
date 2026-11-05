// Which panels the page shows, and in what order. The registry (panels/index.js) lists the panels in their usual order;
// the server's default (`dashboard:` in config.yaml, on <body data-layout>) and this browser's own choice (localStorage,
// edited in the Layout dialog or a panel's own menu) are applied over it:
//   layout = { order: [panel id...], hidden: [panel id...] }
// Unknown ids are ignored and a panel that a layout does not mention keeps its place and is shown, so a stored layout
// never hides a panel added later. A panel that is not available on this installation (no such device, feature off)
// is never mounted and cannot be shown; the dialog lists it apart, with the reason.
//
// Panels with several instances (one card per device): the layout is kept by panel id, and the cards of one panel
// stay together in their panel's place, in the order the panel lists them. A device added or removed later then
// needs no layout change, the `dashboard:` config keeps naming panels, and a stored layout never refers to a device
// that is gone. Each panel's <section data-panel> holds its cards (.panel, data-size 4/8/12 of a 12-column grid);
// layout.js moves sections, and widens the last card of a row that would end in a gap.

import { applyTabs } from './tabs.js';
import { registerActions } from './dispatch.js';
import { icon, keepKeyboard, openModal } from './util.js';
import { showMenu, closeMenus, toast } from './chrome.js';

const STORAGE_KEY = 'pengumon.layout.v1';

let entries = []; // { panel, section, available, reason, count } for every panel whose section is on the page
let defaults = { order: [], hidden: [] }; // registry order with the server's default applied
let layout = { order: [], hidden: [] }; // what is applied now

// `pref` ids first (known ones, once), then the rest of `base` in its order.
function mergeOrder(base, pref) {
  const out = [];
  (Array.isArray(pref) ? pref : []).forEach((id) => { if (base.includes(id) && !out.includes(id)) out.push(id); });
  base.forEach((id) => { if (!out.includes(id)) out.push(id); });
  return out;
}

const idList = (v) => (Array.isArray(v) ? v.filter((id) => typeof id === 'string') : []);

function readJson(text) {
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' ? v : null;
  } catch (e) {
    return null;
  }
}

function readStored() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? readJson(raw) : null;
  } catch (e) {
    return null; // storage blocked or unavailable: the default applies
  }
}

function writeStored() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch (e) {
    // not saved; the layout still applies until the page is closed
  }
}

function clearStored() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (e) {
    // nothing stored that could be removed
  }
}

function combine(ids, server, stored) {
  const known = (list) => idList(list).filter((id) => ids.includes(id));
  const base = { order: mergeOrder(ids, server && server.order), hidden: known(server && server.hidden) };
  if (!stored) return base;
  return { order: mergeOrder(base.order, stored.order), hidden: known(stored.hidden) };
}

const byId = () => new Map(entries.map((e) => [e.panel.id, e]));
const usableOrder = () => layout.order.filter((id) => entries.some((e) => e.panel.id === id && e.available));

// Row packing: cards fill a 12-column row in layout order; the last card of a row that does not reach 12 widens.
function pack() {
  const rows = [];
  const map = byId();
  layout.order.forEach((id) => {
    const e = map.get(id);
    if (!e || !e.available || layout.hidden.includes(id)) return;
    e.section.querySelectorAll(':scope > .panel').forEach((card) => {
      if (card.hidden) return;
      delete card.dataset.span;
      const size = Number(card.dataset.size) || 12;
      let row = rows.find((r) => r.free >= size);
      if (!row) {
        row = { free: 12, cards: [] };
        rows.push(row);
      }
      row.free -= size;
      row.cards.push(card);
    });
  });
  rows.forEach((r) => {
    if (r.free > 0 && r.cards.length) {
      const last = r.cards[r.cards.length - 1];
      last.dataset.span = String((Number(last.dataset.size) || 12) + r.free);
    }
  });
}

// Puts the sections in layout order (the DOM order is also the reading and tab order) and tells tabs.js what is hidden.
function apply() {
  const map = byId();
  layout.order.forEach((id) => {
    const e = map.get(id);
    if (e) e.section.parentElement.appendChild(e.section);
  });
  entries.forEach((e) => { e.section.dataset.layoutHidden = layout.hidden.includes(e.panel.id) ? '1' : '0'; });
  applyTabs();
  pack();
}

function save() {
  writeStored();
  apply();
  renderList();
}

// The order and hidden panels this browser shows now (a copy), for Settings > Display's "Use this browser's layout",
// which writes them into dashboard.order and dashboard.hidden (the layout new browsers start with).
export function browserLayout() {
  return { order: [...layout.order], hidden: [...layout.hidden] };
}

// Panel id -> title, in the panels' standard order (Settings > Display's "Layout for new browsers" names them).
export function panelTitles() {
  return new Map(entries.map((e) => [e.panel.id, e.panel.title]));
}

// Re-packs after a panel added or removed cards (a device panel that changes its size, for example).
export function relayout() {
  pack();
}

function tabLabel(tab) {
  const b = document.querySelector(`#mobileTabs [data-arg="${tab}"]`);
  return b ? b.textContent.trim() : tab;
}

function rowFor(e, at, usable) {
  const id = e.panel.id;
  const row = document.createElement('li');
  const shown = !layout.hidden.includes(id);
  row.className = `lay-row${shown ? '' : ' off'}`;
  const sub = [e.panel.core ? 'Core' : 'Device / optional', `${tabLabel(e.panel.tab)} tab on phones`];
  if (e.count > 1) sub.push(`${e.count} cards`);
  row.innerHTML = `<span class="lay-ic">${icon(e.panel.icon || 'grid')}</span><span class="lay-name"><b></b><span></span></span>`;
  row.querySelector('b').textContent = e.panel.title;
  row.querySelector('.lay-name span').textContent = sub.join(' · ');
  const label = document.createElement('label');
  label.className = 'check lay-show';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = shown;
  box.dataset.change = 'togglePanel';
  box.dataset.arg = id;
  label.append(box, ' Show');
  const move = document.createElement('span');
  move.className = 'lay-move';
  [['up', 'up', at === 0], ['down', 'chevron', at === usable.length - 1]].forEach(([dir, ico, edge]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn icon sm outline';
    b.innerHTML = icon(ico);
    b.disabled = edge;
    b.dataset.action = 'movePanel';
    b.dataset.arg = id;
    b.dataset.arg2 = dir;
    b.setAttribute('aria-label', `Move ${e.panel.title} ${dir}`);
    move.append(b);
  });
  row.append(label, move);
  return row;
}

function renderList() {
  const list = document.getElementById('layoutList');
  if (!list) return;
  const map = byId();
  const usable = usableOrder();
  // the rows are rebuilt (also by a change made in another tab): the control with focus keeps it
  const had = list.contains(document.activeElement) ? document.activeElement : null;
  const key = had ? { arg: had.dataset.arg, arg2: had.dataset.arg2, action: had.dataset.action || had.dataset.change } : null;
  list.replaceChildren(...usable.map((id, at) => rowFor(map.get(id), at, usable)));
  if (key) {
    const same = [...list.querySelectorAll('[data-arg]')].filter((e) => e.dataset.arg === key.arg && (e.dataset.action || e.dataset.change) === key.action && e.dataset.arg2 === key.arg2);
    const target = same.find((e) => !e.disabled) || [...list.querySelectorAll('[data-arg]')].find((e) => e.dataset.arg === key.arg && !e.disabled);
    if (target) target.focus();
    else if (document.getElementById('layoutModal').classList.contains('active')) document.querySelector('#layoutModal .m-close').focus();
  }
  const missing = layout.order.filter((id) => map.has(id) && !map.get(id).available).map((id) => map.get(id));
  const other = document.getElementById('layoutUnavailable');
  document.getElementById('layoutUnavailableTitle').hidden = !missing.length;
  other.replaceChildren(...missing.map((e) => {
    const row = document.createElement('li');
    row.className = 'lay-row na';
    row.innerHTML = `<span class="lay-ic">${icon(e.panel.icon || 'grid')}</span><span class="lay-name"><b></b><span class="layout-reason"></span></span>`;
    row.querySelector('b').textContent = e.panel.title;
    row.querySelector('.layout-reason').textContent = e.reason;
    return row;
  }));
}

function movePanel(id, dir) {
  const usable = usableOrder();
  const from = usable.indexOf(id);
  const to = dir === 'up' ? from - 1 : from + 1;
  if (from < 0 || to < 0 || to >= usable.length) return;
  // swap the two neighbours among the available panels; unavailable ones keep their slots
  const slots = layout.order.map((i, at) => [i, at]).filter(([i]) => usable.includes(i)).map(([, at]) => at);
  const order = layout.order.slice();
  order[slots[from]] = usable[to];
  order[slots[to]] = usable[from];
  layout = { ...layout, order };
  save();
  const keep = document.querySelector(`#layoutList [data-action=movePanel][data-arg="${id}"][data-arg2=${dir}]:not(:disabled)`)
    || document.querySelector(`#layoutList [data-action=movePanel][data-arg="${id}"]:not(:disabled)`);
  if (keep) keep.focus();
}

function togglePanel(id, shown) {
  const hidden = layout.hidden.filter((i) => i !== id);
  if (!shown) hidden.push(id);
  layout = { ...layout, hidden };
  save();
}

function resetLayout() {
  clearStored();
  layout = { order: defaults.order.slice(), hidden: defaults.hidden.slice() };
  apply();
  renderList();
}

// ---------- each card's own menu: move, hide, open the dialog ----------
function panelMenu(button) {
  const id = button.dataset.arg;
  let menu = document.getElementById('panelMenu');
  if (!menu) {
    menu = document.createElement('div');
    menu.id = 'panelMenu';
    menu.className = 'menu fixed';
    menu.setAttribute('role', 'menu');
    menu.hidden = true;
    document.body.appendChild(menu);
  }
  const usable = usableOrder().filter((i) => !layout.hidden.includes(i));
  const at = usable.indexOf(id);
  const item = (action, arg2, ico, text, disabled) => `<button type="button" role="menuitem" data-action="${action}" data-arg="${id}"${arg2 ? ` data-arg2="${arg2}"` : ''}${disabled ? ' disabled' : ''}>${icon(ico)}${text}</button>`;
  menu.innerHTML = item('menuMovePanel', 'up', 'up', 'Move up', at <= 0)
    + item('menuMovePanel', 'down', 'chevron', 'Move down', at < 0 || at >= usable.length - 1)
    + item('hidePanel', '', 'eye-off', 'Hide panel', false)
    + `<hr><button type="button" role="menuitem" data-action="openLayoutDialog">${icon('layout')}Layout…</button>`;
  showMenu(menu, button);
}

// Moves a panel past its next shown neighbour (the menu moves among what is on the page).
function menuMove(id, dir) {
  const shown = usableOrder().filter((i) => !layout.hidden.includes(i));
  const at = shown.indexOf(id);
  const other = shown[dir === 'up' ? at - 1 : at + 1];
  if (at < 0 || !other) return;
  const order = layout.order.slice();
  const a = order.indexOf(id);
  const b = order.indexOf(other);
  [order[a], order[b]] = [order[b], order[a]];
  layout = { ...layout, order };
  save();
}

function addMenuButton(card, panel) {
  const tools = card.querySelector('.panel-h .panel-tools') || card.querySelector('.panel-h')?.appendChild(Object.assign(document.createElement('div'), { className: 'panel-tools' }));
  if (!tools || tools.querySelector('.panel-menu-btn')) return;
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'btn icon sm panel-menu-btn';
  b.dataset.action = 'panelMenu';
  b.dataset.arg = panel.id;
  b.setAttribute('aria-label', `Panel options for ${panel.title}`);
  b.setAttribute('aria-haspopup', 'menu');
  b.setAttribute('aria-expanded', 'false');
  b.innerHTML = icon('more');
  tools.appendChild(b);
}

registerActions('click', {
  openLayoutDialog: () => { closeMenus(); renderList(); keepKeyboard(document.getElementById('layoutModal')); openModal('layoutModal'); },
  movePanel: (el) => movePanel(el.dataset.arg, el.dataset.arg2),
  resetLayout: () => { resetLayout(); toast('Layout reset to the default'); },
  panelMenu: (el) => panelMenu(el),
  menuMovePanel: (el) => menuMove(el.dataset.arg, el.dataset.arg2),
  hidePanel: (el) => {
    const e = byId().get(el.dataset.arg);
    togglePanel(el.dataset.arg, false);
    toast(`${e ? e.panel.title : 'Panel'} hidden. Bring it back from Layout.`);
  }
});
registerActions('change', { togglePanel: (el) => togglePanel(el.dataset.arg, el.checked) });

// Decides which of `panels` this installation has, mounts those, and applies the layout. `ctx` is what a panel gets.
// A panel with `instances(ctx)` gets one mount(section, ctx, instance) call per instance (an empty list: not
// available); one without gets a single mount(section, ctx).
export function mountLayout(panels, ctx) {
  entries = [];
  panels.forEach((panel) => {
    const section = document.querySelector(`[data-panel="${panel.id}"]`);
    if (!section) return;
    if (panel.prepare) panel.prepare(ctx);
    const instances = panel.instances ? panel.instances(ctx) : null;
    const available = instances ? instances.length > 0 : panel.available(ctx);
    section.dataset.tab = panel.tab;
    section.dataset.available = available ? '1' : '0';
    entries.push({
      panel, section, available, instances, count: instances ? instances.length : 1,
      reason: available ? '' : (panel.reason ? panel.reason(ctx) : 'Not available')
    });
  });
  entries.filter((e) => e.available && e.panel.mount).forEach((e) => {
    if (e.instances) e.instances.forEach((inst) => e.panel.mount(e.section, ctx, inst));
    else e.panel.mount(e.section, ctx);
    e.section.querySelectorAll(':scope > .panel').forEach((card) => addMenuButton(card, e.panel));
  });
  const server = readJson(document.body.dataset.layout || '');
  const ids = entries.map((e) => e.panel.id);
  defaults = combine(ids, server, null);
  layout = combine(ids, server, readStored());
  apply();
  // another tab of this browser changed the layout (or reset it): follow it, so two tabs do not disagree until a reload
  window.addEventListener('storage', (ev) => {
    if (ev.key !== STORAGE_KEY && ev.key !== null) return;
    layout = combine(ids, server, readStored());
    apply();
    renderList();
  });
}
