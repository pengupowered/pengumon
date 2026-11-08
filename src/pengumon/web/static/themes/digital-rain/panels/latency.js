// Digital rain's view of the Latency card (docs/themes.md, "Drawing a card"): a terminal pane. The legend is a process
// list down the left (one line per series with its newest reading and, under it, what it is made of), the Path / Each
// target switch is a pair of command-line options, and the standard charts (Chart.js, in the theme's colors and chart
// style) fill the pane beside it. It draws from the card's view model (panels/latency/charts.js) with the standard parts
// (the switch, the charts, the legend's entries): only the layout is this file's. The look is latency.css.

export default {
  mount(root, ctx) {
    const { el, mark, text } = ctx.kit;
    const { legendList, modes, plot } = ctx.std.parts;
    const box = root.appendChild(el('div', 'dr-lat'));
    const bar = box.appendChild(el('div', 'dr-lat-bar'));
    bar.appendChild(el('span', 'dr-prompt', '$ ping')).setAttribute('aria-hidden', 'true');
    const group = bar.appendChild(el('span', 'dr-modes'));
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Lines');
    const note = mark(box.appendChild(el('p', 'dr-lat-note')), 'note');

    const pane = box.appendChild(el('div', 'dr-pane'));
    // one legend entry: a key and the name with the newest reading, then what the series is
    const legend = legendList(pane.appendChild(el('div', 'dr-lg-list')), 'dr', (p) => {
      const entry = el('div', 'dr-lg');
      entry.appendChild(el('span', 'dr-lg-top')).append(...[p.key, p.label, p.now].filter(Boolean));
      if (p.sub) entry.append(p.sub);
      return entry;
    });
    const parts = [legend, modes(group), plot(pane.appendChild(el('div', 'dr-plot')))];

    return {
      update(vm) {
        text(note, vm.note);
        parts.forEach((p) => p.update(vm));
      },
      unmount() { parts.forEach((p) => p.unmount && p.unmount()); }
    };
  }
};
