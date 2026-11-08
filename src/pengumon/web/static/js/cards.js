// Cards drawn by views: the engine that splits a card into its model and its view (docs/themes.md, "Drawing a card").
//
// A card's model (panels/<id>/model.js) fetches, keeps the state, runs the actions and turns its state into a view
// model: plain data with every value already formatted (the words, numbers and units the card shows), statuses as
// {level, word, icon}, actions as {action, arg, label}. Its view draws the card's body (.panel-b) from that view model.
// The standard view (panels/<id>/view.js) is the look every theme gets; a built-in theme may add its own view of a card,
// themes/<theme id>/panels/<panel id>.js (+ .css), found by the server by file presence and listed per theme on
// <html data-looks> (`views`). A view is an ES module whose default export is
//   { mount(root, ctx) -> { update(vm, prev), unmount() } }
// `root` is the card's body, empty for a theme view and the panel.html markup for the standard one. `update` runs for
// every new view model (`prev` is the one it got before, undefined after mount) and again after every theme change
// (views read tokens there); it changes only what changed, so focus, scroll and open rows stay. `unmount` undoes what
// mount did outside `root` (observers, listeners, charts); the engine empties `root` itself.
//
// The engine mounts the current theme's view of a card, or the standard one (js/themes.js loadView loads a theme view
// once per page, only when its theme is in use, and a theme switch waits for the new theme's views, so a card changes view
// before the page is painted in the new theme); swaps views on a theme change without a reload, keeping focus and
// scroll; and replaces a theme view that throws (in mount or update) by the standard view for the rest of the page's
// life, with one warning in the console. The frame of a card (its header and footer, panel.html) is the same in every view: the model keeps it up to
// date and themes restyle it with CSS.
//
// A card that updates often and sits low on the page can choose not to be drawn off screen (createCard's `offscreen:
// 'defer'`): while its body is not on screen, nor near it (a body that is not shown at all, as on another phone tab, is
// off screen), it keeps only its latest view model and is drawn from it when it comes on screen. It is drawn at once
// before it has been drawn at all (it gets its size and rows), while it holds the focus, and before the page is printed.
//
// The marks (collectMarks): every value a view shows carries data-v="<key>" (its aria-label, else its text, else its title),
// every status data-st="<level>" on the element with its icon and word, and rows data-item="<key>" (marks inside are
// named per row). Actions are their data-action (+ data-arg, data-arg2, data-needs-edit) and help their data-help.
// tests/ui/steps/core.mjs mounts the standard view and every theme view of each card on the card's fixture
// (createCard's `fixture`) and fails when a theme view lacks a mark of the standard one.

import { currentLook, loadView, lookInfo } from './themes.js';
import { escapeHtml, icon, iconEl, STATUS } from './util.js';

const cards = new Set(); // every card on the page
const registry = new Map(); // panel id -> { standard, fixture } (the first instance of a panel)
const broken = new Set(); // view URLs that threw: the standard view stands in for them until the page is loaded again
const deferred = new WeakMap(); // body (.panel-b) of a card with offscreen: 'defer' -> its card

// ---------- the kit: helpers every view may use (ctx.kit) ----------
const written = new WeakMap(); // element -> the markup html() wrote last
const signed = new WeakMap(); // list child -> the signature of the item it was drawn from

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// Text, written only when it differs (writing the same text still costs a style and layout pass).
function text(node, value) {
  const v = value === undefined || value === null ? '' : String(value);
  if (node && node.textContent !== v) node.textContent = v;
}

// Markup, written only when it differs from what was written last. Everything in it must be escaped (escapeHtml).
function html(node, markup) {
  if (!node || written.get(node) === markup) return;
  written.set(node, markup);
  node.innerHTML = markup;
}

// An element's value mark: data-v="<key>", and data-st="<level>" when it is a status.
function mark(node, key, level) {
  node.dataset.v = key;
  if (level) node.dataset.st = level;
  return node;
}

// A status as icon + word in `node` (a status is never color alone): data-st="<level>", then the level's icon (or
// `icon`, with the icon class `iconClass`) and `word` (the level's word when none; in a span of class `wordClass` when
// given). Written only when it changed (it is patched on every message); returns `node`.
function status(node, level, word, { icon: name, iconClass = '', wordClass = '' } = {}) {
  const known = STATUS[level] ? level : 'unknown';
  const w = escapeHtml(word ?? STATUS[known].word);
  if (node.dataset.st !== known) node.dataset.st = known;
  html(node, `${icon(name || STATUS[known].icon, iconClass)}${wordClass ? `<span class="${wordClass}">${w}</span>` : w}`);
  return node;
}

// A button or link for an action descriptor {action, arg, arg2, label, icon, needsEdit, href, expanded, primary}.
// `label` is its accessible name; with `text` false only the icon shows (and the label is its tooltip).
function action(a, { className = 'btn sm', text: showText = true } = {}) {
  const node = el(a.href ? 'a' : 'button', className);
  if (a.href) {
    node.href = a.href;
    node.target = '_blank';
    node.rel = 'noopener noreferrer';
  } else {
    node.type = 'button';
    node.dataset.action = a.action;
    if (a.arg !== undefined && a.arg !== null) node.dataset.arg = String(a.arg);
    if (a.arg2 !== undefined && a.arg2 !== null) node.dataset.arg2 = String(a.arg2);
    if (a.needsEdit) node.dataset.needsEdit = '';
    if (a.expanded !== undefined) node.setAttribute('aria-expanded', String(!!a.expanded));
  }
  if (a.icon) node.insertAdjacentHTML('afterbegin', icon(a.icon));
  if (showText) node.append(a.label);
  else {
    node.title = a.label;
    node.setAttribute('aria-label', a.label);
  }
  return node;
}

// What has keyboard focus inside `root`, as something that survives a redraw: its action and arguments, its mark, or
// its place among root's focusable elements.
const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';
function focusKey(root) {
  const had = document.activeElement;
  if (!had || had === document.body || !root.contains(had)) return null;
  const item = had.closest('[data-item]');
  return {
    item: item && root.contains(item) ? item.dataset.item : null,
    action: had.dataset.action || '', arg: had.dataset.arg || '', arg2: had.dataset.arg2 || '', v: had.dataset.v || '',
    at: [...root.querySelectorAll(FOCUSABLE)].indexOf(had)
  };
}
function refocus(root, key) {
  if (!key || root.contains(document.activeElement)) return;
  const all = [...root.querySelectorAll(FOCUSABLE)];
  const same = (n) => (n.dataset.action || '') === key.action && (n.dataset.arg || '') === key.arg && (n.dataset.arg2 || '') === key.arg2
    && (n.dataset.v || '') === key.v && (!key.item || n.closest('[data-item]')?.dataset.item === key.item);
  const target = (key.action || key.v ? all.find(same) : null) || all[Math.min(Math.max(key.at, 0), all.length - 1)];
  if (target) target.focus({ preventScroll: true });
}

// A keyed list: `items` drawn into `container` in order, one child per item (render(item) -> element, given
// data-item=<key> here). A child whose item did not change (sig(item), by default its JSON) is kept as it is; a changed
// one is drawn again and takes the focus its old element had. Children are moved only when the order changed, so an
// element with focus is not taken out of the page.
function list(container, items, { key, render, sig = (item) => JSON.stringify(item) }) {
  const old = new Map([...container.children].filter((c) => c.dataset.item !== undefined).map((c) => [c.dataset.item, c]));
  const want = items.map((item) => {
    const k = String(key(item));
    const s = sig(item);
    let node = old.get(k);
    if (!node || signed.get(node) !== s) {
      const focus = node ? focusKey(node) : null;
      const fresh = render(item);
      fresh.dataset.item = k;
      signed.set(fresh, s);
      if (node) node.replaceWith(fresh);
      node = fresh;
      if (focus) refocus(node, focus);
    }
    old.delete(k);
    return node;
  });
  old.forEach((node) => node.remove());
  want.forEach((node, i) => {
    if (container.children[i] !== node) container.insertBefore(node, container.children[i] || null);
  });
}

export const kit = { el, text, html, mark, status, action, list, icon, iconEl, escapeHtml, STATUS };

// ---------- the marks a view must keep (the parity test) ----------
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();

// Every mark in `root` that is shown (not inside a [hidden] element): { values: {key: text}, status: {key: level and
// whether it has an icon}, actions: [action|arg|arg2|edit], help: [key] }. Keys inside a row are "<row key>/<key>".
export function collectMarks(root) {
  const shown = (n) => { const h = n.closest('[hidden]'); return !h || !root.contains(h) || h === root; };
  const scope = (n) => {
    const rows = [];
    for (let p = n.parentElement?.closest('[data-item]'); p && root.contains(p); p = p.parentElement?.closest('[data-item]')) rows.unshift(p.dataset.item);
    if (n.dataset.item !== undefined && n !== root) rows.push(n.dataset.item);
    return rows.length ? `${rows.join('/')}/` : '';
  };
  const out = { values: {}, status: {}, actions: [], help: [] };
  root.querySelectorAll('[data-v]').forEach((n) => {
    if (!shown(n)) return;
    const k = scope(n) + n.dataset.v;
    // what it says: its accessible name when it has one (an icon link), else its text, else its tooltip
    if (!(k in out.values)) out.values[k] = norm(n.getAttribute('aria-label')) || norm(n.textContent) || norm(n.getAttribute('title'));
  });
  root.querySelectorAll('[data-st]').forEach((n) => {
    if (!shown(n)) return;
    const v = n.dataset.v !== undefined ? n : n.querySelector('[data-v]');
    const k = scope(n) + (v ? v.dataset.v : 'status');
    out.status[k] = `${n.dataset.st}${n.querySelector('svg') ? '' : ' (no icon)'}`;
  });
  root.querySelectorAll('[data-action]').forEach((n) => {
    if (!shown(n)) return;
    const d = n.dataset;
    out.actions.push(`${scope(n)}${d.action}|${d.arg || ''}|${d.arg2 || ''}${d.needsEdit !== undefined ? '|edit' : ''}`);
  });
  root.querySelectorAll('[data-help]').forEach((n) => { if (shown(n)) out.help.push(n.dataset.help); });
  out.actions = [...new Set(out.actions)].sort();
  out.help = [...new Set(out.help)].sort();
  return out;
}

// What `theirs` (a theme view's marks) lacks of `ours` (the standard view's), in words; [] when nothing is missing.
export function compareMarks(ours, theirs) {
  const problems = [];
  Object.entries(ours.values).forEach(([k, v]) => {
    if (!(k in theirs.values)) problems.push(`value ${k} (${JSON.stringify(v)}) is missing`);
    else if (theirs.values[k] !== v) problems.push(`value ${k} shows ${JSON.stringify(theirs.values[k])}, not ${JSON.stringify(v)}`);
  });
  Object.entries(ours.status).forEach(([k, v]) => {
    if (theirs.status[k] !== v) problems.push(`status ${k} is ${theirs.status[k] ? JSON.stringify(theirs.status[k]) : 'missing'}, not ${JSON.stringify(v)}`);
  });
  ours.actions.filter((a) => !theirs.actions.includes(a)).forEach((a) => problems.push(`action ${a} is missing`));
  ours.help.filter((h) => !theirs.help.includes(h)).forEach((h) => problems.push(`help ${h} is missing`));
  return problems;
}

// ---------- views ----------
// The URL of the current (or a given) theme's view of a panel, '' for the standard view.
export function viewUrl(panel, look = currentLook()) {
  const info = lookInfo(look);
  return (info && info.views && info.views[panel]) || '';
}

function failed(url, e) {
  if (broken.has(url)) return;
  broken.add(url);
  console.warn(`A theme's card view failed, the standard view is shown instead (${url}): ${e && e.message ? e.message : e}`);
}

// The context a view gets: the panel, the look it draws for, the card (<article>, its frame included: read it, do not
// change it), the standard view (its `parts` are the standard part renderers, to reuse), the kit and the view model now.
function context(c, look) {
  return { panel: c.panel, look, card: c.card, section: c.section, std: c.standard, kit, vm: () => c.vm };
}

// Puts `view` (from `url`; '' is the standard view) into the card's body.
function mount(c, view, url) {
  const focus = focusKey(c.root);
  const scroll = c.root.scrollTop;
  if (c.inst) {
    try { if (c.inst.unmount) c.inst.unmount(); } catch (e) { if (c.url) failed(c.url, e); else throw e; }
  }
  c.inst = null;
  if (c.drawn || url) c.root.replaceChildren(...(url ? [] : c.pristine.cloneNode(true).childNodes));
  c.drawn = true;
  c.url = url;
  const look = url ? currentLook() : 'standard';
  c.root.dataset.view = look;
  if (c.card) c.card.dataset.view = look;
  c.prev = undefined;
  try {
    c.inst = view.mount(c.root, context(c, look)) || {};
    if (c.vm !== undefined) draw(c);
  } catch (e) {
    if (!url) throw e;
    failed(url, e);
    mount(c, c.standard, '');
    return;
  }
  if (scroll) c.root.scrollTop = scroll;
  refocus(c.root, focus);
}

// True when the view drew the card's view model (false: no view yet, or nothing to draw before the first view model).
function draw(c) {
  if (!c.inst || !c.inst.update || c.vm === undefined) return false;
  c.stale = false;
  c.inst.update(c.vm, c.prev);
  c.prev = c.vm;
  return true;
}

// Draws the card's latest view model; a theme view that throws gives way to the standard view (which draws it).
function redraw(c) {
  try { return draw(c); } catch (e) {
    if (!c.url) throw e;
    failed(c.url, e);
    mount(c, c.standard, '');
    return true;
  }
}

// Cards with offscreen: 'defer': whether each is on screen (or within 200 px of it), and the drawing they wait for.
const near = typeof IntersectionObserver === 'function' ? new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    const c = deferred.get(e.target);
    c.shown = e.isIntersecting;
    if (c.shown && c.stale) redraw(c);
  });
}, { rootMargin: '200px 0px' }) : null;
window.addEventListener('beforeprint', () => cards.forEach((c) => { if (c.stale) redraw(c); }));

// Shows the view the current theme asks for (no change when it is already shown).
function choose(c) {
  let url = viewUrl(c.panel);
  if (broken.has(url)) url = '';
  if (c.inst && url === c.url) return false;
  const seq = ++c.seq;
  if (!url) { mount(c, c.standard, ''); return true; }
  if (!c.inst) c.root.dataset.view = 'loading'; // hidden until its view is there (components.css)
  loadView(url).then((view) => { // loaded already on a theme switch (themes.js): mounted before the next paint
    if (seq !== c.seq) return; // the theme changed again meanwhile
    if (!view) { broken.add(url); mount(c, c.standard, ''); return; }
    mount(c, view, url);
  });
  return true;
}

// The model's handle on its card. `root`: the card's body (.panel-b); `standard`: the standard view module (default
// export of panels/<id>/view.js); `fixture`: () -> a view model for the parity test, made from fixed data by the model's
// own code (the first instance of a panel registers it); `offscreen`: 'defer' for a card that is not drawn while it is
// off screen (see the top of this file).
export function createCard({ panel, root, standard, fixture, offscreen }) {
  const c = {
    panel, root, standard, card: root.closest('.panel'), section: root.closest('[data-panel]'),
    pristine: root.cloneNode(true), inst: null, url: '', vm: undefined, prev: undefined, seq: 0, drawn: false,
    shown: true, stale: false // shown: on screen (always, unless the card defers); stale: a view model waits to be drawn
  };
  cards.add(c);
  if (offscreen === 'defer' && near) {
    deferred.set(root, c);
    near.observe(root);
  }
  if (!registry.has(panel)) registry.set(panel, { standard, fixture: fixture || null });
  choose(c);
  return {
    // a new view model: drawn by the view now (or as soon as its view is loaded; a card that defers: when it is on
    // screen). True when it was drawn now.
    update(vm) {
      c.vm = vm;
      if (!c.shown && c.prev !== undefined && !c.root.contains(document.activeElement)) { c.stale = true; return false; }
      return redraw(c);
    },
    get vm() { return c.vm; },
    get look() { return c.root.dataset.view; }
  };
}

// The cards on this page that registered a fixture: [{ panel, standard, fixture }] (the parity test reads it).
export function registeredCards() {
  return [...registry].map(([panel, r]) => ({ panel, ...r }));
}

// A view drawn away from the page from a view model, for the parity test: an offscreen copy of the card's body markup
// (ids taken out, so the page's own elements keep theirs) for the standard view, an empty body for a theme view.
// Returns { root, marks, remove() }.
export function drawApart(panel, view, vm, { look = 'standard' } = {}) {
  const r = registry.get(panel);
  const live = [...cards].find((c) => c.panel === panel);
  if (!r || !live) throw new Error(`no card ${panel} on the page`);
  const box = el('div');
  box.style.cssText = `position: fixed; left: -10000px; top: 0; width: ${Math.max(live.root.clientWidth, 320)}px; visibility: hidden;`;
  box.setAttribute('aria-hidden', 'true');
  const card = live.card ? live.card.cloneNode(false) : el('article', 'panel');
  const root = live.pristine.cloneNode(view === r.standard); // the body markup for the standard view, an empty body for a theme view
  [card, root, ...root.querySelectorAll('[id]')].forEach((n) => n.removeAttribute('id'));
  root.dataset.view = look;
  card.dataset.view = look;
  card.append(root);
  box.append(card);
  document.body.append(box);
  const inst = view.mount(root, { panel, look, card, section: null, std: r.standard, kit, vm: () => vm }) || {};
  if (inst.update) inst.update(vm, undefined);
  const marks = collectMarks(root);
  return { root, marks, remove() { try { if (inst.unmount) inst.unmount(); } finally { box.remove(); } } };
}

// A theme change: each card shows the new theme's view (or the standard one), and every view draws again (tokens).
// One card that fails does not stop the others (nor the code that changed the theme).
document.addEventListener('themechange', () => {
  cards.forEach((c) => {
    try {
      if (choose(c)) return;
      try { draw(c); } catch (e) {
        if (!c.url) throw e;
        failed(c.url, e);
        mount(c, c.standard, '');
      }
    } catch (e) {
      console.error(`The ${c.panel} card could not be drawn: ${e && e.message ? e.message : e}`);
    }
  });
});
