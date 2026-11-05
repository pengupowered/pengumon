// What the list editors share: cards (a summary line, "Edit" expands), Add, Remove, Move up/down, a message under the
// field the server's validation names, and unique ids. An editor is
//   { id, render(container, items, ctx), collect() -> items, problems(errors) }
// ctx: { schema, listName, heading ('group' | 'sub' | 'none'), savedIds(), markDirty(), canEdit(), openDeviceSetup(id) }.

import { registerActions } from '../../../js/dispatch.js';
import { escapeHtml, icon, slug } from '../../../js/util.js';
import { clone } from '../model.js';

export { slug };

// The buttons of a card (Move, Remove, Add, Reset, and an editor's own) name `data-action="settingsListAct"`, the list in
// data-arg and what they do in data-la; each editor registers its click handler here under its list id.
const clickers = new Map();
registerActions('click', { settingsListAct: (el) => { const click = clickers.get(el.dataset.arg); if (click) click(el); } });

// A free id: `base`, then base_2, base_3, ...
export function freshId(base, taken) {
  const root = (slug(base) || 'item').slice(0, 56);
  let id = root;
  for (let k = 2; taken.includes(id); k++) id = `${root}_${k}`;
  return id;
}

// One labelled input of a card. `cfg`: {f, label, kind: text|number|bool|choice|id, choices, labels (choice -> words),
// nullable, hint, placeholder}.
export function fieldHtml(cfg, item, index, locked) {
  const id = `lf-${cfg.list}-${index}-${cfg.f}`;
  const v = item[cfg.f];
  const attrs = `id="${id}" data-f="${cfg.f}" data-kind="${cfg.kind}"${cfg.nullable ? ' data-nullable' : ''}`;
  let control;
  if (cfg.kind === 'bool') {
    control = `<label class="check"><input type="checkbox" ${attrs}${v ? ' checked' : ''}> ${escapeHtml(cfg.label)}</label>`;
    return `<div class="set-f set-f-bool">${control}<p class="set-msg" data-lmsg="${index}.${cfg.f}" role="alert"></p></div>`;
  }
  if (cfg.kind === 'choice') {
    control = `<select class="input" ${attrs}>${cfg.choices.map((c) => `<option value="${escapeHtml(c)}"${c === v ? ' selected' : ''}>${escapeHtml((cfg.labels && cfg.labels[c]) || c)}</option>`).join('')}</select>`;
  } else {
    const ro = locked ? ' readonly aria-readonly="true"' : '';
    control = `<input class="input${cfg.kind === 'number' ? ' num' : ''}${cfg.mono ? ' mono' : ''}" type="text" ${cfg.kind === 'number' ? 'inputmode="decimal" ' : ''}autocomplete="off" spellcheck="false" ${attrs}${ro} value="${escapeHtml(v ?? '')}" placeholder="${escapeHtml(cfg.placeholder || '')}">`;
  }
  return `<div class="set-f"><label for="${id}">${escapeHtml(cfg.label)}${cfg.required ? '' : ' <span class="muted">(optional)</span>'}</label>${control}
    ${cfg.hint ? `<span class="hint" data-hint="${cfg.f}"${cfg.hintIf && !cfg.hintIf(item) ? ' hidden' : ''}>${escapeHtml(cfg.hint)}</span>` : ''}<p class="set-msg" data-lmsg="${index}.${cfg.f}" role="alert"></p></div>`;
}

// What a card's input holds as the item's value.
export function readField(el) {
  const kind = el.dataset.kind;
  if (kind === 'bool') return el.checked;
  const text = el.value.trim();
  if (kind === 'number') {
    if (text === '') return el.hasAttribute('data-nullable') ? null : '';
    const n = Number(text);
    return Number.isFinite(n) ? n : text;
  }
  if (text === '' && el.hasAttribute('data-nullable')) return null;
  return text;
}

// cfg: {id (list name), label, noun, empty, fields: [field cfg], blank(items, ctx) -> new item, title(item), summary(item),
// ordered, extra(item, index, ctx) -> html in the card body, after(el, item, index) hook for a changed field, ...}
export function makeListEditor(cfg) {
  let items = [];
  let box = null;
  let ctx = null;
  const wired = new WeakSet(); // the page builds a fresh container each time Settings opens
  const open = new Set();
  let errors = {};

  const saved = (item) => ctx.savedIds().includes(item.id);

  function cardHtml(item, i) {
    const locked = saved(item) && cfg.idLocked !== false;
    const fields = cfg.fields.map((f) => fieldHtml({ ...f, list: cfg.id }, item, i, f.f === 'id' && locked)).join('');
    const isOpen = open.has(item.id);
    const off = item.enabled === false ? '<span class="chip">Paused</span>' : '';
    const bad = Object.keys(errors).some((k) => k.startsWith(`${i}.`)) ? '<span class="chip st st-crit">Needs a fix</span>' : '';
    const flags = (cfg.chips ? cfg.chips(item, i) : '');
    return `<details class="set-card" data-i="${i}" data-id="${escapeHtml(item.id)}"${isOpen ? ' open' : ''}>
      <summary>${icon('chevron', 'set-chev')}<span class="set-card-t">${escapeHtml(cfg.title(item) || item.id || 'New')}</span><span class="set-card-s">${escapeHtml(cfg.summary(item) || '')}</span>${off}${flags}${bad}</summary>
      <div class="set-card-b">
        <div class="set-grid">${fields}</div>${cfg.extra ? cfg.extra(item, i, ctx) : ''}
        <div class="set-card-f">
          ${cfg.ordered ? `<button type="button" class="btn icon sm" data-action="settingsListAct" data-arg="${cfg.id}" data-la="up" data-i="${i}" data-needs-edit aria-label="Move ${escapeHtml(item.name || item.id)} up"${i === 0 ? ' disabled' : ''}>${icon('up')}</button>
          <button type="button" class="btn icon sm set-down" data-action="settingsListAct" data-arg="${cfg.id}" data-la="down" data-i="${i}" data-needs-edit aria-label="Move ${escapeHtml(item.name || item.id)} down"${i === items.length - 1 ? ' disabled' : ''}>${icon('up')}</button>` : ''}
          <button type="button" class="btn sm danger" data-action="settingsListAct" data-arg="${cfg.id}" data-la="remove" data-i="${i}" data-needs-edit>${icon('close')}Remove</button>
        </div>
      </div></details>`;
  }

  // The list's name at the level of the place it sits in (see ctx.heading); none where the group above names it.
  function heading() {
    if (ctx.heading === 'none') return '';
    return ctx.heading === 'sub' ? `<h5 class="set-sub">${escapeHtml(cfg.label)}</h5>` : `<h4 class="set-group-t">${escapeHtml(cfg.label)}</h4>`;
  }

  function draw() {
    box.innerHTML = `<div class="set-list" data-list="${cfg.id}">
      <div class="set-list-h">${heading()}${ctx.listIsSet() && cfg.resetLabel ? `<button type="button" class="set-lnk" data-action="settingsListAct" data-arg="${cfg.id}" data-la="reset" data-needs-edit>${escapeHtml(cfg.resetLabel)}</button>` : ''}</div>
      ${cfg.note ? `<p class="fine top">${escapeHtml(cfg.note)}</p>` : ''}
      ${items.length ? items.map(cardHtml).join('') : `<p class="fine top set-empty">${escapeHtml(cfg.empty)}</p>`}
      <div><button type="button" class="btn sm outline" data-action="settingsListAct" data-arg="${cfg.id}" data-la="add" data-needs-edit>${icon('plus')}<span>${escapeHtml(cfg.addLabel || `Add ${cfg.noun}`)}</span></button></div></div>`;
    showErrors();
  }

  function showErrors() {
    box.querySelectorAll('[data-lmsg]').forEach((p) => { p.textContent = errors[p.dataset.lmsg] || ''; });
  }

  function wire() {
    if (wired.has(box)) return;
    wired.add(box);
    clickers.set(cfg.id, (b) => {
      if (!box.contains(b) || !ctx.canEdit()) return;
      const i = Number(b.dataset.i);
      const act = b.dataset.la;
      if (act === 'add') {
        const item = cfg.blank(items, ctx);
        items.push(item);
        open.add(item.id);
      } else if (act === 'remove') {
        const [gone] = items.splice(i, 1);
        open.delete(gone.id);
        if (cfg.removed) cfg.removed(gone, ctx);
      } else if (act === 'up' || act === 'down') {
        const j = act === 'up' ? i - 1 : i + 1;
        if (j < 0 || j >= items.length) return;
        [items[i], items[j]] = [items[j], items[i]];
      } else if (act === 'reset') {
        ctx.resetList();
        return;
      } else if (cfg.action && cfg.action(act, b, items, ctx, { draw, open })) {
        return;
      }
      errors = {};
      ctx.markDirty();
      draw();
    });
    const edited = (ev) => {
      const el = ev.target instanceof Element ? ev.target.closest('[data-f]') : null;
      if (!el || !ctx.canEdit()) return;
      const card = el.closest('.set-card');
      const i = Number(card.dataset.i);
      const item = items[i];
      const f = el.dataset.f;
      const before = item.id;
      item[f] = readField(el);
      const rename = (from, to) => { if (open.delete(from)) open.add(to); };
      const t = card.querySelector('.set-card-t');
      if (t) t.textContent = cfg.title(item) || item.id || 'New';
      const s = card.querySelector('.set-card-s');
      if (s) s.textContent = cfg.summary(item) || '';
      if (cfg.after) cfg.after(el, item, i, card, ev.type, ctx, items, rename);
      if (item.id !== before) { card.dataset.id = item.id; rename(before, item.id); } // the id was typed over, or a hook derived it
      ctx.markDirty();
    };
    box.addEventListener('input', edited);
    box.addEventListener('change', edited);
    box.addEventListener('toggle', (ev) => {
      const card = ev.target;
      if (!(card instanceof Element) || !card.classList.contains('set-card')) return;
      if (card.open) open.add(card.dataset.id); else open.delete(card.dataset.id);
    }, true);
  }

  return {
    id: cfg.id,
    render(container, its, cx) {
      box = container;
      ctx = cx;
      items = clone(its);
      errors = {};
      wire();
      draw();
    },
    collect: () => clone(items),
    // the server's answers for this list, {"0.url": "message"}, keyed by item index and field
    problems(byWhere) {
      errors = byWhere || {};
      if (box) { showErrors(); box.querySelectorAll('.set-card').forEach((c) => {
        const bad = Object.keys(errors).some((k) => k.startsWith(`${c.dataset.i}.`));
        const sum = c.querySelector('summary');
        const chip = sum.querySelector('.st-crit');
        if (bad && !chip) sum.insertAdjacentHTML('beforeend', '<span class="chip st st-crit">Needs a fix</span>');
        if (!bad && chip && chip.textContent === 'Needs a fix') chip.remove();
        if (bad) c.open = true;
      }); }
    },
    redraw() { if (box) draw(); }
  };
}
