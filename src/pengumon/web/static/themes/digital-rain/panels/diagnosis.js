// Digital rain's view of the Path health card (docs/themes.md, "Drawing a card"): a terminal readout in the manner of a
// route trace. The verdict is a shell status line; the path a tree, each hop a two-line entry (index, label, status and
// the big reading, then name, loss, jitter and the last probes as block characters) with the latency each segment adds
// on the branch before it. It draws from the card's view model (panels/diagnosis/model.js) with the standard parts (the
// notice, and the verdict, hop fields and chips for a layout of its own): only the layout is this file's. The look is
// diagnosis.css.

export default {
  mount(root, ctx) {
    const { el, mark, list, text } = ctx.kit;
    const { notice, verdictLine, hopFields, extraChips, blockTrace } = ctx.std.parts;
    const box = root.appendChild(el('div', 'dr-diag'));
    box.appendChild(el('p', 'dr-prompt', '$ trace --from this-host --every 1s')).setAttribute('aria-hidden', 'true');
    const verdict = verdictLine(box, 'dr'); // a status line, then the sentence as a comment

    const banner = box.appendChild(el('div', 'path-banner dr-banner'));
    banner.setAttribute('role', 'status');
    banner.setAttribute('aria-live', 'polite');
    banner.hidden = true;
    const bannerPart = notice(banner);

    const path = box.appendChild(el('div', 'dr-path'));
    const origin = path.appendChild(el('p', 'dr-origin'));
    origin.append('00  this host · probing ', mark(el('b', 'dr-count'), 'hostCount'), ' targets · every 1 s');
    const count = origin.querySelector('.dr-count');
    const tree = path.appendChild(el('ol', 'dr-tree'));
    tree.setAttribute('aria-label', 'Network path from this host to the internet');
    const none = mark(el('p', 'dr-none'), 'none');
    const extra = extraChips(box, 'dr', '# also watched');

    const foot = box.appendChild(el('p', 'dr-key'));
    foot.append('# ', el('span', 'dr-busy'), 'big number: median round trip, last 60 s · blocks: last probes, × = lost');
    const busy = foot.querySelector('.dr-busy');

    // one hop: the branch with what its segment adds, then the two-line entry
    const hops = hopFields('dr');
    const refs = new WeakMap();
    const build = () => {
      const li = el('li', 'dr-entry');
      const f = hops.make();
      const link = li.appendChild(el('div', 'dr-link'));
      link.appendChild(el('span', 'dr-pipe', '│')).setAttribute('aria-hidden', 'true');
      link.append(f.delta);
      f.idx = el('span', 'dr-idx');
      f.idx.setAttribute('aria-hidden', 'true');
      f.spark = el('span', 'dr-spark');
      f.spark.setAttribute('aria-hidden', 'true');
      f.hop.append(f.idx, f.kind, f.st, f.val, f.name, f.meta, f.spark);
      li.append(f.hop);
      refs.set(li, f);
      return li;
    };
    const patch = (li, n, i) => {
      const f = refs.get(li);
      hops.patch(f, n);
      text(f.idx, String(i + 1).padStart(2, '0'));
      text(f.spark, blockTrace(n.spark, 30));
    };

    return {
      update(vm, prev = {}) {
        if (vm.verdict !== prev.verdict) verdict.update(vm.verdict);
        if (vm.banner !== prev.banner) bannerPart.update(vm.banner);
        if (vm.path) {
          const p = vm.path;
          text(count, p.hostCount);
          if (!p.nodes.length) { if (!none.isConnected) path.append(none); text(none, p.none); tree.replaceChildren(); } else {
            if (none.isConnected) none.remove();
            list(tree, p.nodes, { key: (n) => n.key, render: build, sig: () => '' });
            p.nodes.forEach((n, i) => patch(tree.children[i], n, i));
          }
          if (p.extras !== (prev.path && prev.path.extras)) extra.update(p.extras);
        }
        if (vm.busyLegend !== prev.busyLegend) text(busy, vm.busyLegend ? 'busy = expected under load · ' : '');
      },
      unmount() {}
    };
  }
};
