// Synthwave's view of the Path health card (docs/themes.md, "Drawing a card"): neon outlined readouts standing on the
// horizon grid. The verdict is a lit sign; each hop a glowing tile (label and status on top, the reading large, a neon
// trace of the last probes at the bottom) with the latency its segment adds as a tag on the tile's edge. It draws from
// the card's view model (panels/diagnosis/model.js) with the standard parts (the notice, and the verdict, hop fields and
// chips for a layout of its own): only the layout is this file's. The look is diagnosis.css.

const NS = 'http://www.w3.org/2000/svg';

// The last probes as a neon trace: a polyline for the readings, a short tick at the floor for each lost probe.
function drawTrace(svg, values) {
  const v = (values || []).slice(-60);
  const top = Math.max(1, ...v.filter((x) => x !== null && x !== undefined));
  const step = v.length > 1 ? 100 / (v.length - 1) : 0;
  const run = [];
  const ticks = [];
  v.forEach((x, i) => {
    if (x === null || x === undefined) { ticks.push(i * step); return; }
    run.push(`${(i * step).toFixed(1)},${(20 - (x / top) * 17).toFixed(1)}`);
  });
  const key = `${run.join(' ')}|${ticks.join(',')}`;
  if (svg.dataset.key === key) return;
  svg.dataset.key = key;
  svg.replaceChildren();
  const line = document.createElementNS(NS, 'polyline');
  line.setAttribute('points', run.join(' '));
  line.setAttribute('class', 'sw-trace-l');
  svg.append(line);
  ticks.forEach((x) => {
    const t = document.createElementNS(NS, 'line');
    t.setAttribute('x1', x.toFixed(1));
    t.setAttribute('x2', x.toFixed(1));
    t.setAttribute('y1', '20');
    t.setAttribute('y2', '24');
    t.setAttribute('class', 'sw-trace-x');
    svg.append(t);
  });
}

export default {
  mount(root, ctx) {
    const { el, mark, list, text } = ctx.kit;
    const { notice, verdictLine, hopFields, extraChips } = ctx.std.parts;
    const box = root.appendChild(el('div', 'sw-diag'));
    const verdict = verdictLine(box, 'sw'); // a lit sign

    const banner = box.appendChild(el('div', 'path-banner sw-banner'));
    banner.setAttribute('role', 'status');
    banner.setAttribute('aria-live', 'polite');
    banner.hidden = true;
    const bannerPart = notice(banner);

    const host = box.appendChild(el('p', 'sw-host'));
    host.append('This host · probing ', mark(el('b', 'sw-count'), 'hostCount'), ' targets every 1 s');
    const count = host.querySelector('.sw-count');

    // the horizon: the tiles stand on the grid
    const floor = box.appendChild(el('div', 'sw-floor'));
    const tiles = floor.appendChild(el('ol', 'sw-tiles'));
    tiles.setAttribute('aria-label', 'Network path from this host to the internet');
    const none = mark(el('p', 'sw-none'), 'none');
    const extra = extraChips(box, 'sw', 'Also watched');

    const foot = box.appendChild(el('p', 'sw-key'));
    foot.append(el('span', 'sw-busy'), 'Big number: median round trip, last 60 s · trace: last probes, a tick at the floor is a lost probe');
    const busy = foot.querySelector('.sw-busy');

    // one hop: a tile (label and status on top, the reading, name and figures, the trace) with what its segment adds on
    // its edge
    const hops = hopFields('sw', 'sw-tile');
    const refs = new WeakMap();
    const build = () => {
      const li = el('li', 'sw-cell');
      const f = hops.make();
      const top = el('span', 'sw-top');
      top.append(f.kind, f.st);
      f.svg = document.createElementNS(NS, 'svg');
      f.svg.setAttribute('viewBox', '0 0 100 24');
      f.svg.setAttribute('preserveAspectRatio', 'none');
      f.svg.setAttribute('aria-hidden', 'true');
      f.svg.setAttribute('class', 'sw-trace');
      f.hop.append(top, f.val, f.name, f.meta, f.svg);
      li.append(f.hop, f.delta);
      refs.set(li, f);
      return li;
    };
    const patch = (li, n) => {
      const f = refs.get(li);
      hops.patch(f, n);
      li.className = `sw-cell st-${n.level}`;
      drawTrace(f.svg, n.spark);
    };

    return {
      update(vm, prev = {}) {
        if (vm.verdict !== prev.verdict) verdict.update(vm.verdict);
        if (vm.banner !== prev.banner) bannerPart.update(vm.banner);
        if (vm.path) {
          const p = vm.path;
          text(count, p.hostCount);
          if (!p.nodes.length) { if (!none.isConnected) floor.append(none); text(none, p.none); tiles.replaceChildren(); } else {
            if (none.isConnected) none.remove();
            list(tiles, p.nodes, { key: (n) => n.key, render: build, sig: () => '' });
            p.nodes.forEach((n, i) => patch(tiles.children[i], n));
          }
          if (p.extras !== (prev.path && prev.path.extras)) extra.update(p.extras);
        }
        if (vm.busyLegend !== prev.busyLegend) text(busy, vm.busyLegend ? 'Busy = expected under load · ' : '');
      },
      unmount() {}
    };
  }
};
