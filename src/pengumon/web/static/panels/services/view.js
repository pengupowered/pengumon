// Host services card, the standard view: one row per service (status, name and address or the reason it fails, time,
// Open / Copy / History, the facts and the outages), drawn from model.js's view model. Rows are keyed by service: a
// row is drawn again only when its service changed, and the "checked Ns ago" facts are rewritten in place.
// docs/themes.md, "Drawing a card"; a theme's own view of this card gets these parts as ctx.std.parts.

import { kit } from '../../js/cards.js';

const { el, icon, mark } = kit;
const ICON_ONLY = { className: 'btn icon sm', text: false }; // an icon button: its label is its tooltip and name

// The facts line, "checked 12s ago · up since 2.0h ago": each fact its own mark.
function facts(box, list) {
  const now = list.map((f) => f.text).join(' · ');
  if (box.textContent === now) return;
  box.replaceChildren();
  list.forEach((f, i) => {
    if (i) box.append(' · ');
    box.append(mark(el('span', null, f.text), f.key));
  });
}

// The outages under an open History: when, how long, what.
function history(r) {
  const box = el('div', 'svc-history');
  box.dataset.role = 'history';
  if (!r.outages.length) {
    box.appendChild(mark(el('span', null, 'No outages recorded.'), 'outages'));
    return box;
  }
  r.outages.forEach((o, i) => {
    const line = el('div', 'svc-history-item');
    line.dataset.item = `outage-${i}`;
    line.append(mark(el('strong', null, o.when), 'when'), ' ', mark(el('span', null, o.length), 'length'), ' ', mark(el('span', null, o.text), 'text'));
    box.appendChild(line);
  });
  return box;
}

// One service's row.
function row(r) {
  const node = el('li', `svc-row st-${r.status.level}`);
  node.dataset.service = r.id;

  const st = el('span', 'svc-st');
  st.dataset.st = r.status.level;
  st.innerHTML = icon(r.status.icon);
  const word = st.appendChild(mark(el('span', null, r.status.word), 'status'));
  word.dataset.role = 'status';
  node.appendChild(st);

  const id = el('span', 'svc-id svc-head');
  id.appendChild(mark(el('b', 'svc-name', r.name), 'name'));
  if (r.reason) {
    const why = mark(el('span', 'svc-why svc-reason', r.reason.text), 'reason');
    why.dataset.kind = r.reason.kind;
    id.appendChild(why);
  } else {
    id.appendChild(mark(el('span', 'mono svc-check-url', r.checkUrl), 'checkUrl'));
  }
  node.appendChild(id);
  node.appendChild(mark(el('span', 'svc-ms num svc-ms-col', r.ms), 'ms'));

  const actions = el('span', 'svc-actions');
  if (r.open) actions.appendChild(mark(kit.action(r.open, { ...ICON_ONLY, className: 'btn icon sm svc-open' }), 'open'));
  actions.append(kit.action(r.copy, ICON_ONLY), kit.action(r.history, ICON_ONLY));
  node.appendChild(actions);

  const more = el('div', 'svc-more');
  if (r.description) more.appendChild(mark(el('span', null, r.description), 'description'));
  facts(more.appendChild(el('span', 'svc-facts')), r.facts);
  if (r.reason) more.appendChild(mark(el('span', 'mono svc-check-url', r.checkUrl), 'checkUrl'));
  if (r.outages) more.appendChild(history(r));
  node.appendChild(more);
  return node;
}

// What makes a row draw again: everything but its facts (those change every few seconds and are written in place).
const sig = (r) => JSON.stringify({ ...r, facts: undefined });

// The list of rows (a part: another view can put it in a box of its own).
function rows(list) {
  return {
    update(vm) {
      kit.list(list, vm.rows, { key: (r) => r.id, render: row, sig });
      vm.rows.forEach((r, i) => facts(list.children[i].querySelector('.svc-facts'), r.facts));
    },
    unmount() {}
  };
}

export const parts = { row, rows, facts, history };

export default {
  parts,
  mount(root) {
    return rows(root.querySelector('.svc'));
  }
};
