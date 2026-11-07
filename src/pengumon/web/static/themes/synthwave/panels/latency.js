// Synthwave's view of the Latency card (docs/themes.md, "Drawing a card"): neon readouts over a glowing screen. Each
// series is an outlined readout in its own colour (its name, the newest reading large, what it is made of under it), the
// Path / Each target switch is a pair of neon toggles, and the standard charts (Chart.js, in the theme's colours and
// chart style) sit in a frame lit pink to cyan. It draws from the card's view model (panels/latency/charts.js) with the
// standard parts (the switch, the charts, the legend's entries): only the layout is this file's. The look is
// latency.css.

export default {
  mount(root, ctx) {
    const { el, mark, text } = ctx.kit;
    const { legendList, modes, plot } = ctx.std.parts;
    const box = root.appendChild(el('div', 'sw-lat'));

    const head = box.appendChild(el('div', 'sw-lat-head'));
    // one readout, in its line's colour: the name, the newest reading, what the series is made of
    const legend = legendList(head.appendChild(el('div', 'sw-lg-list')), 'sw', (p, it) => {
      const entry = el('div', `sw-lg${it.kind === 'line' ? '' : ' minor'}`);
      entry.appendChild(el('span', 'sw-lg-top')).append(p.key, p.label);
      entry.append(...[p.now, p.sub].filter(Boolean));
      return entry;
    });
    const group = head.appendChild(el('span', 'sw-modes'));
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Lines');
    const note = mark(box.appendChild(el('p', 'sw-lat-note')), 'note');

    const screen = box.appendChild(el('div', 'sw-screen'));
    const parts = [legend, modes(group), plot(screen)];

    return {
      update(vm) {
        text(note, vm.note);
        parts.forEach((p) => p.update(vm));
      },
      unmount() { parts.forEach((p) => p.unmount && p.unmount()); }
    };
  }
};
