// Reliability report, the standard view: the headline, downtime and incident count, a bar per day of what was lost, the
// downtime by cause and a table of every hop, drawn from model.js's view model. The frame is built once; each part is
// written only when it changed (so the open "Every hop" stays open when a hop starts to ignore ping). docs/themes.md,
// "Drawing a card"; a theme's own view of this card gets these parts as ctx.std.parts.

import { kit } from '../../js/cards.js';

const { escapeHtml: esc } = kit;

const SKELETON = '<div class="skel" style="height:44px;width:60%;margin-bottom:14px"></div><div class="skel-stack"><div class="skel line" style="width:92%"></div><div class="skel line" style="width:76%"></div><div class="skel line" style="width:84%"></div></div>';

const stat = (key, s) => `<div class="stat"><span class="lbl">${esc(s.label)}</span><span class="val num" data-v="${key}">${esc(s.value)}</span>`
  + (s.sub ? `<span class="sub" data-v="outages">${esc(s.sub)}</span>` : '') + '</div>';

// The headline with the downtime and the incident count.
function hero(box, vm) {
  kit.html(box, `<div class="stat"><span class="lbl">${esc(vm.hero.label)}</span><span class="val hero-num num" data-v="availability">${esc(vm.hero.value)}<small>${esc(vm.hero.unit)}</small></span>`
    + `<span class="sub" title="${esc(vm.hero.subTitle)}" data-v="source">${esc(vm.hero.sub)}</span></div>`
    + `<div class="rep-mini">${stat('downtime', vm.downtime)}${stat('incidents', vm.incidents)}</div>`);
}

// The bar per day.
function days(box, vm) {
  const l = vm.lost;
  kit.html(box, `<h3 class="rb-t">${esc(l.title)}</h3>`
    + `<div class="days" style="grid-template-columns: repeat(${l.columns}, 1fr)" role="img" aria-label="${esc(l.aria)}">${l.bars.map((b) => `<div class="day${b.nodata ? ' nodata' : ''}" data-item="day-${esc(b.id)}" data-v="tip" aria-label="${esc(b.tip)}" data-tip="${esc(b.tip)}"><i style="height:${b.height.toFixed(0)}%"></i><span>${esc(b.letter)}</span></div>`).join('')}</div>`
    + `<p class="rep-note" data-v="worstDay">${esc(l.note)}</p>`);
}

// The downtime by cause.
function causes(box, vm) {
  const c = vm.causes;
  kit.html(box, `<h3 class="rb-t">${esc(c.title)}</h3><ul class="causes">${c.items.map((i) => `<li title="${esc(i.title)}" data-item="cause-${esc(i.id)}"><span data-v="cause">${esc(i.label)}</span><span class="cbar"><i style="width:${i.share}%"></i></span><b class="num" data-v="share">${esc(i.shareText)}</b></li>`).join('')
    || `<li class="muted" data-v="none">${esc(c.empty)}</li>`}</ul>`);
}

// The table of every hop.
function hops(body, vm) {
  const h = vm.hops;
  kit.html(body, h.rows.map((r) => `<tr data-item="hop-${esc(r.id)}"><td class="lead"><span class="primary" title="${esc(r.name.title)}" data-v="name">${esc(r.name.text)}</span></td>`
    + (r.quiet ? `<td class="r aside muted" title="${esc(r.availTitle)}" data-v="availability">${esc(r.avail)}</td>` : `<td class="r num aside" data-v="availability">${esc(r.avail)}</td>`)
    + `<td class="r num" data-l="Worst day" data-v="worstDay">${esc(r.worst)}</td></tr>`).join('')
    || `<tr><td colspan="3" class="muted" data-v="none">${esc(h.empty)}</td></tr>`);
}

export const parts = { hero, days, causes, hops };

export default {
  parts,
  mount(root) {
    let built = null; // the frame's elements once a report is drawn
    const frame = (vm) => {
      root.innerHTML = '<div class="rep-grid"><div class="rep-hero"></div><div class="rep-days"></div><div class="rep-causes"></div></div><p class="rep-note" hidden></p>'
        + `<details class="rep-hops-box"><summary class="fine"></summary><table class="dlist rep-hops"><thead><tr><th>${esc(vm.hops.columns[0])}</th><th class="r">${esc(vm.hops.columns[1])}</th><th class="r">${esc(vm.hops.columns[2])}</th></tr></thead><tbody></tbody></table></details>`;
      built = { hero: root.querySelector('.rep-hero'), days: root.querySelector('.rep-days'), causes: root.querySelector('.rep-causes'), excluded: root.querySelector(':scope > .rep-note'),
        summary: root.querySelector('summary'), body: root.querySelector('tbody') };
      built.excluded.dataset.v = 'excluded';
    };
    return {
      update(vm) {
        if (vm.state !== 'ready') {
          built = null;
          if (vm.state === 'loading') root.innerHTML = SKELETON;
          else root.textContent = vm.error;
          return;
        }
        if (!built) frame(vm);
        hero(built.hero, vm);
        days(built.days, vm);
        causes(built.causes, vm);
        built.excluded.hidden = !vm.excluded;
        kit.text(built.excluded, vm.excluded || '');
        kit.text(built.summary, vm.hops.summary);
        hops(built.body, vm);
      },
      unmount() {}
    };
  }
};
