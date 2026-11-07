// Amber CRT's view of the Incidents card (docs/themes.md, "Drawing a card"): a terminal log. A prompt line, the standard
// 7-day timeline, then one line per incident (`when  [STATUS]  what  duration  cause`) where a line opens the incident's
// details. It draws from the card's view model (panels/incidents/model.js) and reuses the standard timeline. The look is
// incidents.css.

export default {
  mount(root, ctx) {
    const { el, mark, icon, list, status, text, html, escapeHtml: esc } = ctx.kit;
    const { timeline } = ctx.std.parts;
    const box = root.appendChild(el('div', 'ac-inc'));
    box.appendChild(el('p', 'ac-prompt', '> TAIL -F INCIDENTS.LOG')).setAttribute('aria-hidden', 'true');

    const tl = box.appendChild(el('div', 'tl ac-tl'));
    tl.hidden = true;

    // the bar naming the time another panel picked; the model focuses it (data-role="window")
    const bar = box.appendChild(el('div', 'ac-win'));
    bar.dataset.role = 'window';
    bar.tabIndex = -1;
    bar.hidden = true;
    const winLabel = mark(el('b'), 'windowLabel');
    const winFound = mark(el('span'), 'windowFound');
    bar.append('-- window: ', winLabel, ' · ', winFound, ' ');
    const clear = bar.appendChild(el('button', 'ac-clear'));
    clear.type = 'button';

    const head = box.appendChild(el('div', 'ac-cols'));
    ['WHEN', 'STATUS', 'WHAT', 'DURATION', 'CAUSE'].forEach((c) => head.appendChild(el('span', null, c)));
    const help = head.appendChild(el('button', 'info ac-help'));
    help.type = 'button';
    help.dataset.action = 'toggleHelp';
    help.dataset.help = 'incident.cause';
    help.setAttribute('aria-label', 'About the cause and load');
    help.innerHTML = icon('info');
    [...head.querySelectorAll('span')].forEach((s) => s.setAttribute('aria-hidden', 'true'));

    const log = box.appendChild(el('div', 'ac-log'));
    log.setAttribute('role', 'list');
    log.setAttribute('aria-label', 'Incidents');
    const parts = [timeline(tl)];

    const line = (r) => {
      const row = el('div', `ac-ln st-${r.status.level}${r.expected ? ' expected' : ''}`);
      row.setAttribute('role', 'listitem');
      row.dataset.action = r.open.action;
      row.dataset.arg = r.open.arg;
      const when = mark(el('span', 'ac-when', r.when.text), 'when');
      when.title = r.when.title;
      const st = status(mark(el('span', 'ac-stt'), 'status'), r.status.level, r.status.word, { icon: r.status.icon });
      const what = mark(el('button', 'ac-what', r.title), 'title');
      what.type = 'button';
      what.dataset.action = r.open.action;
      what.dataset.arg = r.open.arg;
      what.title = r.open.label;
      const dur = mark(el('span', `ac-dur${r.duration.lost ? ' lost' : ''}`, r.duration.text), 'duration');
      if (r.duration.title) dur.title = r.duration.title;
      row.append(when, st, what, dur, mark(el('span', 'ac-cause', r.cause), 'cause'));
      return row;
    };

    return {
      update(vm, prev) {
        parts.forEach((p) => p.update(vm, prev));
        bar.hidden = !vm.window;
        if (vm.window) {
          text(winLabel, vm.window.label);
          text(winFound, vm.window.found);
          const c = vm.window.clear;
          clear.dataset.action = c.action;
          html(clear, `${icon(c.icon)}${esc(c.label)}`);
        }
        const l = vm.list;
        const empty = log.querySelector('.ac-empty');
        if (!l.rows.length) {
          const sentence = `<div class="ac-empty" data-v="empty">${icon(l.empty.icon)}<span><b>${esc(l.empty.head)}</b>${esc(l.empty.text)}</span></div>`;
          if (!empty || log.dataset.empty !== sentence) { log.dataset.empty = sentence; log.innerHTML = sentence; }
          return;
        }
        if (empty) { delete log.dataset.empty; empty.remove(); }
        list(log, l.rows, { key: (r) => r.id, render: line });
      },
      unmount() { parts.forEach((p) => p.unmount()); }
    };
  }
};
