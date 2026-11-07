// The view side of what every device card (router, line) shares: the error block. Used by the standard views of
// panels/router and panels/line, and a theme's view may reuse it (ctx.std.parts.error). The view model part is
// device-panel.js errorOf(): {tone, icon, title, text, setup} or null.

import { kit } from './cards.js';

const { escapeHtml, html, icon, mark } = kit;

// Fills `box` (the card's error block) from the view model's error: hidden when null. Marked as one status (the whole
// text is its value) with the fix as its action.
export function errorPart(box) {
  return {
    update(e) {
      box.hidden = !e;
      if (!e) return;
      const unset = e.tone === 'unset';
      box.classList.add('empty');
      box.classList.toggle('st', !unset);
      box.classList.toggle('st-crit', !unset);
      mark(box, 'error', unset ? 'unknown' : 'crit');
      const fix = e.setup
        ? `<div class="row-btns"><button type="button" class="btn sm${e.setup.primary ? ' primary' : ' outline'}" data-action="${escapeHtml(e.setup.action)}" data-needs-edit data-arg="${escapeHtml(e.setup.arg)}">${icon(e.setup.icon)}${escapeHtml(e.setup.label)}</button></div>`
        : '';
      html(box, `${icon(e.icon)}<div><b>${escapeHtml(e.title)}</b>${escapeHtml(e.text)}${fix}</div>`);
    },
    unmount() {}
  };
}
