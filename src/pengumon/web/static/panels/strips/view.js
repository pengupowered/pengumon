// Uptime, the standard view: a strip of blocks per target (every block a button that lists the incidents of its time), its
// availability, the time axis and the key, drawn from model.js's view model. The grid's markup is written only when the
// view model changed, and the block that had the keyboard focus gets it back. docs/themes.md, "Drawing a card".

import { kit } from '../../js/cards.js';
import { numText } from '../../js/escape.js';

const { escapeHtml: esc } = kit;

// The grid: head, a row (name, strip, availability) per target, the axis.
function markup(vm) {
  if (vm.state === 'empty') return `<div class="empty" style="grid-column: 1 / -1"><b data-v="empty">${esc(vm.empty)}</b></div>`;
  const rows = vm.rows.map((r) => {
    const sub = r.sub ? `<small data-v="fullName">${esc(r.sub)}</small>` : '';
    const head = `<button type="button" class="s-name" data-item="name-${esc(r.id)}" data-action="openTargetHistory" data-arg="${esc(r.id)}" title="${esc(r.name)}"><span data-v="label">${esc(r.short)}</span>${sub}</button>`;
    if (r.quiet) {
      return `${head}<div class="strip-quiet" data-item="strip-${esc(r.id)}" data-v="summary" role="img" title="${esc(r.quiet.why)}" aria-label="${esc(r.quiet.aria)}">${esc(r.quiet.strip)}</div>`
        + `<span class="s-pct num" data-item="pct-${esc(r.id)}" data-v="availability" title="${esc(r.quiet.pctTitle)}">${esc(r.pct)}</span>`;
    }
    const cells = r.cells.map((c, i) => `<button type="button" class="cell ${c.cls}" tabindex="${i ? -1 : 0}" data-item="${c.id}" data-v="tip" data-action="showIncidentWindow" data-start="${numText(c.start)}" data-end="${numText(c.end)}"`
      + ` data-label="${esc(c.label)}" data-state="${esc(c.state)}" data-tip="${esc(c.tip)}" aria-label="${esc(c.aria)}"></button>`).join('');
    return `${head}<div class="strip" style="--n:${r.cells.length}" data-item="strip-${esc(r.id)}" data-v="summary" role="group" data-roving aria-label="${esc(r.aria)}">${cells}</div>`
      + `<span class="s-pct num" data-item="pct-${esc(r.id)}" data-v="availability">${esc(r.pct)}</span>`;
  }).join('');
  return `<span class="s-hd">${esc(vm.columns[0])}</span><span class="s-hd">${esc(vm.columns[1])}</span><span class="s-hd r">${esc(vm.columns[2])}</span>${rows}`
    + `<span></span><div class="s-axis" data-v="axis">${vm.axis.map((a) => `<span>${esc(a)}</span>`).join('')}</div><span></span>`;
}

// The grid of strips (a part: another view can draw it into a box of its own).
function grid(box) {
  return {
    update(vm) {
      const had = document.activeElement && box.contains(document.activeElement) ? document.activeElement : null;
      const focus = had ? { item: had.dataset.item, strip: had.closest('.strip')?.dataset.item } : null;
      if (vm.state === 'error') kit.html(box, `<div class="empty" style="grid-column: 1 / -1" data-v="error">${esc(vm.error)}</div>`);
      else kit.html(box, markup(vm));
      if (vm.state === 'ready') box.dataset.blocks = String(vm.blocks); else delete box.dataset.blocks;
      if (focus && !box.contains(document.activeElement)) {
        const strip = focus.strip ? [...box.querySelectorAll('.strip')].find((s) => s.dataset.item === focus.strip) : box;
        (strip && strip.querySelector(`[data-item="${focus.item}"]`))?.focus({ preventScroll: true });
      }
    },
    unmount() {}
  };
}

export const parts = { grid };

export default {
  parts,
  mount(root) {
    const g = grid(root.querySelector('#stripsBody, .strips'));
    const items = root.querySelectorAll('.strip-legend > span:not(.grow)');
    items.forEach((s, i) => kit.mark(s, `legend-${i}`));
    const note = kit.mark(root.querySelector('.strip-legend > .grow'), 'legendNote');
    return {
      update(vm) {
        g.update(vm);
        if (!vm.legend) return; // an error has no key: the old one stays
        vm.legend.items.forEach((item, i) => {
          if (items[i] && items[i].lastChild.textContent !== item.word) items[i].lastChild.textContent = item.word;
        });
        kit.text(note, vm.legend.note);
      },
      unmount() { g.unmount(); }
    };
  }
};
