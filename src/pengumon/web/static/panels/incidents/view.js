// Incidents card, the standard view: the 7-day timeline (a lane per cause, a bar per incident), the bar naming the time
// another panel picked, and the table of incidents (one line each), drawn from model.js's view model. Rows are keyed by
// incident: a row is drawn again only when its incident changed; the timeline is written only when it changed and the bar
// that had the keyboard focus gets it back. docs/themes.md, "Drawing a card"; a theme's own view of this card gets these
// parts as ctx.std.parts.

import { kit } from '../../js/cards.js';
import { numText } from '../../js/escape.js';

const { el, escapeHtml: esc, icon, mark } = kit;

// ---------- the timeline ----------

function timelineMarkup(t) {
  const span = (value) => `${value.toFixed(2)}%`;
  return t.lanes.map((lane) => `<div class="tl-lane" data-item="lane-${esc(lane.key)}"><span class="tl-l" data-v="lane">${esc(lane.word)}</span><div class="tl-track">${lane.bars.map((b) => `<button type="button" class="tl-bar st-${b.level}${b.expected ? ' exp' : ''}${b.ongoing ? ' ongoing' : ''}"`
    + ` style="left:${span(b.left)};width:${span(b.width)}" data-item="bar-${numText(b.id)}" data-v="tip" data-action="inspectIncident" data-arg="${numText(b.id)}" data-tip="${esc(b.tip)}" aria-label="${esc(b.aria)}"></button>`).join('')}</div></div>`).join('')
    + `<div class="tl-axis"><span></span><div data-v="days">${t.days.map((d) => `<span>${esc(d)}</span>`).join('')}</div></div>`
    + `<div class="tl-now" style="--now:${t.now.toFixed(4)}"></div>`
    + (t.window ? `<div class="tl-win" style="--a:${t.window.from.toFixed(4)};--b:${t.window.to.toFixed(4)}" aria-hidden="true"></div>` : '');
}

// The timeline in its box (a part: another view can draw it into a box of its own).
function timeline(box) {
  return {
    update(vm) {
      box.hidden = !vm.timeline;
      if (!vm.timeline) return;
      const had = document.activeElement && box.contains(document.activeElement) ? document.activeElement.dataset.item : null;
      kit.html(box, timelineMarkup(vm.timeline));
      if (had && !box.contains(document.activeElement)) box.querySelector(`[data-item="${had}"]`)?.focus({ preventScroll: true });
    },
    unmount() {}
  };
}

// ---------- the table ----------

// One incident's row: the title is the row's button (so the keyboard reaches it), a click anywhere on the row does the same.
// An outage of 2 or more drops is a row of its own whose button lists or hides its drops (aria-expanded); its drops follow it as
// incident rows (`drop`: a little in from the left).
function row(r) {
  const outage = r.kind === 'outage';
  const tr = el('tr', `row-click incident-row${r.expected ? ' expected' : ''}${outage ? ' outage-row' : ''}${r.kind === 'drop' ? ' drop-row' : ''}`);
  tr.dataset.action = r.open.action;
  tr.dataset.arg = r.open.arg;
  const lead = outage
    ? `<button type="button" class="primary outage-toggle" data-v="title" data-action="${r.toggle.action}" data-arg="${esc(r.toggle.arg)}" aria-expanded="${r.toggle.expanded}" aria-label="${esc(r.toggle.label)}" title="${esc(r.toggle.label)}">${icon('chevron')}<span>${esc(r.title)}</span></button>`
      + `<span class="inc-sub"><span data-v="summary">${esc(r.summary)}</span>`
      + (r.state ? ` <span class="inc-state" data-v="state">${esc(r.state)}</span>` : '')
      + (r.maintenance ? ` <span class="chip maint" data-v="maintenance" title="${esc(r.maintenance.title)}">${esc(r.maintenance.text)}</span>` : '') + '</span>'
    : `<button type="button" class="primary" data-v="title" data-action="${r.open.action}" data-arg="${esc(r.open.arg)}" title="${esc(r.open.label)}">${esc(r.title)}</button>`;
  tr.innerHTML = `
    <td class="lead">${lead}</td>
    <td class="aside"><span class="badge st-${r.status.level}${r.status.quiet ? ' quiet' : ''}" data-v="status" data-st="${r.status.level}">${icon(r.status.icon)}${esc(r.status.word)}</span></td>
    <td class="num" data-l=""><span class="when" data-v="when" title="${esc(r.when.title)}">${esc(r.when.text)}</span></td>
    <td class="num r"><span class="dur${r.duration.lost ? ' lost' : ''}" data-v="duration"${r.duration.title ? ` title="${esc(r.duration.title)}"` : ''}>${esc(r.duration.text)}</span></td>
    <td class="hide-narrow"><span class="chip" data-v="cause">${esc(r.cause)}</span></td>
    <td class="r hide-narrow go" aria-hidden="true">${icon('right')}</td>`;
  return tr;
}

// A row is drawn again when it changed; its drops are rows of their own, so they are not part of an outage row's signature.
const rowSig = (r) => JSON.stringify(r.kind === 'outage' ? { ...r, drops: undefined } : r);

// The rows of the table body, or the sentence that says why there are none (a part: another view can draw them into a
// table body of its own).
function rows(tbody) {
  return {
    update(vm) {
      const l = vm.list;
      if (!l.rows.length) {
        const sentence = `<tr><td colspan="6"><div class="empty" data-v="empty">${icon(l.empty.icon)}<div><b>${esc(l.empty.head)}</b>${esc(l.empty.text)}</div></div></td></tr>`;
        if (tbody.dataset.empty !== sentence) { tbody.dataset.empty = sentence; tbody.innerHTML = sentence; }
        return;
      }
      delete tbody.dataset.empty;
      [...tbody.children].filter((c) => c.dataset.item === undefined).forEach((c) => c.remove()); // the empty sentence
      kit.list(tbody, l.rows, { key: (r) => r.id, render: row, sig: rowSig });
    },
    unmount() {}
  };
}

// The bar naming the time another panel picked.
function windowBar(bar) {
  const label = mark(bar.querySelector('[data-f=label]'), 'windowLabel');
  const found = mark(bar.querySelector('[data-f=found]'), 'windowFound');
  return {
    update(vm) {
      bar.hidden = !vm.window;
      if (!vm.window) return;
      kit.text(label, vm.window.label);
      kit.text(found, vm.window.found);
    },
    unmount() {}
  };
}

export const parts = { timeline, rows, row, windowBar };

export default {
  parts,
  mount(root) {
    const pieces = [timeline(root.querySelector('#incidentTimeline, .tl')), windowBar(root.querySelector('#incidentWindow, .inc-window')), rows(root.querySelector('#incidentsBody, .inc-list tbody'))];
    return {
      update(vm, prev) { pieces.forEach((p) => p.update(vm, prev)); },
      unmount() { pieces.forEach((p) => p.unmount()); }
    };
  }
};
