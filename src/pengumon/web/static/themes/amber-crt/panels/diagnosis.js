// Amber CRT's view of the Path health card (docs/themes.md, "Drawing a card"): a green-screen-era terminal. The verdict
// is a status line, the path a monospace table (one line per hop: bracketed status, ASCII bar `[#####.....]`, figures
// in columns, the latency each segment adds, the last probes as block characters). It draws from the card's view model
// (panels/diagnosis/model.js) with the standard parts (the notice, and the verdict, hop fields and chips for a layout of
// its own): only the layout is this file's. The look is diagnosis.css.

const CELLS = 10;

// `[#####.....]`: the latency on a 0 to 50 ms scale (a reading above that fills the bar)
const bar = (num) => {
  const ms = parseFloat(String(num || '').replace(/,/g, ''));
  const full = Number.isFinite(ms) ? Math.min(CELLS, Math.max(1, Math.round(ms / 5))) : 0;
  return `[${'#'.repeat(full)}${'.'.repeat(CELLS - full)}]`;
};

export default {
  mount(root, ctx) {
    const { el, mark, list, text } = ctx.kit;
    const { notice, verdictLine, hopFields, extraChips, blockTrace } = ctx.std.parts;
    const box = root.appendChild(el('div', 'ac-diag'));
    box.appendChild(el('p', 'ac-prompt', '> PATH --TRACE --HOST=THIS')).setAttribute('aria-hidden', 'true');
    const verdict = verdictLine(box, 'ac');

    // the notice over the path (first run, route changed)
    const banner = box.appendChild(el('div', 'path-banner ac-banner'));
    banner.setAttribute('role', 'status');
    banner.setAttribute('aria-live', 'polite');
    banner.hidden = true;
    const bannerPart = notice(banner);

    const host = box.appendChild(el('p', 'ac-host'));
    host.append('HOST  this host, probing ', mark(el('b', 'ac-count'), 'hostCount'), ' targets every 1 s');
    const count = host.querySelector('.ac-count');

    const path = box.appendChild(el('ol', 'ac-path'));
    path.setAttribute('aria-label', 'Network path from this host to the internet');
    const none = mark(el('li', 'ac-none'), 'none');
    const extra = extraChips(box, 'ac', 'Also watched');

    const foot = box.appendChild(el('p', 'ac-key'));
    foot.append('[OK] healthy  [WARN] degraded  [DOWN] down  ', el('span', 'ac-busy', ''), '+1.0 ms = latency the segment adds');
    const busy = foot.querySelector('.ac-busy');

    // one hop: a line of the table
    const hops = hopFields('ac');
    const refs = new WeakMap();
    const build = () => {
      const li = el('li', 'ac-line');
      const f = hops.make();
      f.bar = el('span', 'ac-bar');
      f.spark = el('span', 'ac-spark');
      f.bar.setAttribute('aria-hidden', 'true');
      f.spark.setAttribute('aria-hidden', 'true');
      f.hop.append(f.st, f.kind, f.name, f.bar, f.val, f.meta, f.delta, f.spark);
      li.append(f.hop);
      refs.set(li, f);
      return li;
    };
    const patch = (li, n) => {
      const f = refs.get(li);
      hops.patch(f, n);
      text(f.bar, bar(n.value.num));
      text(f.spark, blockTrace(n.spark, 24));
    };

    return {
      update(vm, prev = {}) {
        if (vm.verdict !== prev.verdict) verdict.update(vm.verdict);
        if (vm.banner !== prev.banner) bannerPart.update(vm.banner);
        if (vm.path) {
          const p = vm.path;
          text(count, p.hostCount);
          if (!p.nodes.length) { if (!none.isConnected) path.replaceChildren(none); text(none, p.none); } else {
            if (none.isConnected) none.remove();
            list(path, p.nodes, { key: (n) => n.key, render: build, sig: () => '' });
            p.nodes.forEach((n, i) => patch(path.children[i], n));
          }
          if (p.extras !== (prev.path && prev.path.extras)) extra.update(p.extras);
        }
        if (vm.busyLegend !== prev.busyLegend) text(busy, vm.busyLegend ? '[BUSY] expected under load  ' : '');
      },
      unmount() {}
    };
  }
};
