// Synthwave's view of the Reliability card (docs/themes.md, "Drawing a card"): a neon dashboard. The availability is a
// large lit readout with downtime and incidents as outlined readouts beside it, the lost share per day neon columns
// standing on a grid, the downtime by cause glowing meters, and the hop table under a fold. It draws from the card's view
// model (panels/report/model.js); the frame is built once and each part is written only when it changed (so the open
// "Every hop" stays open). The look is report.css.

export default {
  mount(root, ctx) {
    const { escapeHtml: esc, html, text } = ctx.kit;
    let built = null;
    const readout = (key, s) => `<div class="sw-read"><span class="sw-lbl">${esc(s.label)}</span><span class="sw-rv num" data-v="${key}">${esc(s.value)}</span>${s.sub ? `<span class="sw-rs" data-v="outages">${esc(s.sub)}</span>` : ''}</div>`;

    const frame = (vm) => {
      root.innerHTML = '<div class="sw-rep"><div class="sw-hero"></div><div class="sw-days"></div><div class="sw-causes"></div></div><p class="sw-excl" hidden></p>'
        + `<details class="sw-hops-box"><summary></summary><table class="sw-hops"><thead><tr><th>${esc(vm.hops.columns[0])}</th><th class="r">${esc(vm.hops.columns[1])}</th><th class="r">${esc(vm.hops.columns[2])}</th></tr></thead><tbody></tbody></table></details>`;
      built = { hero: root.querySelector('.sw-hero'), days: root.querySelector('.sw-days'), causes: root.querySelector('.sw-causes'), excl: root.querySelector('.sw-excl'),
        summary: root.querySelector('summary'), body: root.querySelector('tbody') };
      built.excl.dataset.v = 'excluded';
    };

    return {
      update(vm) {
        if (vm.state !== 'ready') {
          built = null;
          if (vm.state === 'loading') root.innerHTML = '<p class="sw-wait">Loading report...</p>';
          else root.textContent = vm.error;
          return;
        }
        if (!built) frame(vm);
        const h = vm.hero;
        html(built.hero, `<div class="sw-big"><span class="sw-lbl">${esc(h.label)}</span><span class="sw-bn num" data-v="availability">${esc(h.value)}<small>${esc(h.unit)}</small></span>`
          + `<span class="sw-src" title="${esc(h.subTitle)}" data-v="source">${esc(h.sub)}</span></div>`
          + `<div class="sw-reads">${readout('downtime', vm.downtime)}${readout('incidents', vm.incidents)}</div>`);
        const l = vm.lost;
        html(built.days, `<h3 class="sw-t">${esc(l.title)}</h3>`
          + `<div class="sw-cols" style="grid-template-columns: repeat(${Number(l.columns)}, 1fr)" role="img" aria-label="${esc(l.aria)}">${l.bars.map((b) => `<div class="sw-col${b.nodata ? ' nodata' : ''}" data-item="day-${esc(b.id)}" data-v="tip" aria-label="${esc(b.tip)}" title="${esc(b.tip)}"><i style="height:${Number(b.height).toFixed(0)}%"></i><span>${esc(b.letter)}</span></div>`).join('')}</div>`
          + `<p class="sw-note" data-v="worstDay">${esc(l.note)}</p>`);
        const c = vm.causes;
        html(built.causes, `<h3 class="sw-t">${esc(c.title)}</h3><ul class="sw-meters">${c.items.map((i) => `<li title="${esc(i.title)}" data-item="cause-${esc(i.id)}"><span class="sw-mn" data-v="cause">${esc(i.label)}</span><b class="sw-mp num" data-v="share">${esc(i.shareText)}</b><span class="sw-bar"><i style="width:${Number(i.share)}%"></i></span></li>`).join('')
          || `<li class="sw-muted" data-v="none">${esc(c.empty)}</li>`}</ul>`);
        built.excl.hidden = !vm.excluded;
        text(built.excl, vm.excluded || '');
        text(built.summary, vm.hops.summary);
        html(built.body, vm.hops.rows.map((r) => `<tr data-item="hop-${esc(r.id)}"><td class="sw-hn"><span title="${esc(r.name.title)}" data-v="name">${esc(r.name.text)}</span></td>`
          + (r.quiet ? `<td class="r sw-muted" title="${esc(r.availTitle)}" data-v="availability">${esc(r.avail)}</td>` : `<td class="r num" data-v="availability">${esc(r.avail)}</td>`)
          + `<td class="r num" data-v="worstDay">${esc(r.worst)}</td></tr>`).join('')
          || `<tr><td colspan="3" class="sw-muted" data-v="none">${esc(vm.hops.empty)}</td></tr>`);
      },
      unmount() {}
    };
  }
};
