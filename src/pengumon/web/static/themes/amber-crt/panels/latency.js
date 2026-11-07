// Amber CRT's view of the Latency card (docs/themes.md, "Drawing a card"): the chart on a terminal screen. A prompt line,
// the legend as a bracketed list with the newest reading of each line, the Path / Each target switch as bracketed
// choices, and the standard charts (Chart.js, drawn in the theme's colours and chart style) in a bezel with corner
// marks. It draws from the card's view model (panels/latency/charts.js) with the standard parts (the switch, the charts,
// the legend's entries): only the layout is this file's. The look is latency.css.

export default {
  mount(root, ctx) {
    const { el, mark, text } = ctx.kit;
    const { legendList, modes, plot } = ctx.std.parts;
    const box = root.appendChild(el('div', 'ac-lat'));
    box.appendChild(el('p', 'ac-prompt', '> PING --WATCH --WINDOW')).setAttribute('aria-hidden', 'true');

    const head = box.appendChild(el('div', 'ac-lat-head'));
    // one legend entry, bracketed: a key, the line's name, what it is made of and its newest reading
    const legend = legendList(head.appendChild(el('div', 'ac-lg-list')), 'ac', (p) => {
      const entry = el('span', 'ac-lg');
      entry.append('[', ...[p.key, p.label, p.sub, p.now].filter(Boolean), ']');
      return entry;
    });
    const group = head.appendChild(el('span', 'ac-modes'));
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Lines');
    const note = mark(box.appendChild(el('p', 'ac-lat-note')), 'note');

    const screen = box.appendChild(el('div', 'ac-screen'));
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
