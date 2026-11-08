// When it breaks, the standard view: the grid of hours (a button for every hour that has begun), the scale, the hint and
// the sentence, drawn from model.js's view model. The grid's markup is written only when it changed; the hour that had
// the keyboard focus gets it back. docs/themes.md, "Drawing a card".

import { kit } from '../../js/cards.js';

const { escapeHtml } = kit;

// The grid as markup: day names and hours are marked (data-item per hour, data-v="tip" is what a cell says). A cell's
// fill is --f (an hour to come is i.later), so a theme can restyle the cells and keep their colors.
function gridMarkup(vm) {
  const head = `<span></span>${vm.hours.map((h) => `<span class="hd">${h}</span>`).join('')}`;
  const rows = vm.rows.map((row) => `<span data-item="day-${row.id}" data-v="day">${escapeHtml(row.name)}</span>${row.cells.map((c) => {
    const fill = c.q === null ? 'var(--nodata)' : `var(--q${c.q})`;
    if (c.future) return `<i class="later" style="--f:${fill}" data-item="${c.id}" data-v="tip" aria-label="${escapeHtml(c.aria)}" data-tip="${escapeHtml(c.tip)}"></i>`;
    return `<button type="button" class="cell" tabindex="${c.id === '0-0' ? 0 : -1}" style="--f:${fill}" data-item="${c.id}" data-v="tip" data-action="showIncidentWindow" data-start="${c.start}" data-end="${c.end}"`
      + ` data-label="${escapeHtml(c.label)}" data-lost="${c.lost}" data-sent="${c.sent}" data-tip="${escapeHtml(c.tip)}" aria-label="${escapeHtml(c.aria)}"></button>`;
  }).join('')}`).join('');
  return head + rows;
}

// The sentence: its bold parts as <b>.
function insightMarkup(insight) {
  return insight.parts.map((p) => (p.strong ? `<b>${escapeHtml(p.text)}</b>` : escapeHtml(p.text))).join('');
}

// The grid of hours (a part: another view can draw it into a box of its own, with the day and hour cells it marks).
function grid(box) {
  return {
    update(vm) {
      const had = document.activeElement && box.contains(document.activeElement) ? document.activeElement.dataset.item : null;
      if (vm.state === 'error') {
        kit.html(box, `<div class="empty" style="grid-column: 1 / -1" data-v="error">${escapeHtml(vm.error)}</div>`);
        return;
      }
      kit.html(box, gridMarkup(vm));
      box.setAttribute('aria-label', vm.label);
      if (had) box.querySelector(`button[data-item="${had}"]`)?.focus({ preventScroll: true });
    },
    unmount() {}
  };
}

export const parts = { grid };

export default {
  parts,
  mount(root) {
    const g = grid(root.querySelector('#heatGrid, .heat'));
    const hint = root.querySelector('#heatHint, .heat-foot .fine');
    const insight = root.querySelector('#heatInsight, .insight');
    const scale = root.querySelectorAll('.scale-legend > span');
    kit.mark(hint, 'hint');
    kit.mark(scale[0], 'scaleLow');
    kit.mark(scale[1], 'scaleHigh');
    insight.querySelector('span').dataset.v = 'insight';
    return {
      update(vm, prev) {
        g.update(vm, prev);
        if (vm.state !== 'ready') return;
        kit.text(scale[0], vm.legend.low);
        kit.text(scale[1], vm.legend.high);
        hint.hidden = !vm.hint.shown;
        kit.text(hint, vm.hint.text);
        insight.hidden = false;
        kit.html(insight.querySelector('span'), insightMarkup(vm.insight));
      },
      unmount() { g.unmount(); }
    };
  }
};
