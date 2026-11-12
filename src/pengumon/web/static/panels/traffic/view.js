// Internet use card, the standard view: the notice when the router gives no traffic figures, the totals, the hourly chart
// (Chart.js, chart.js) with its range buttons and the sentences about the hours the router has not published, the devices that
// used the most (a button each: it opens that device's dialog) and the apps of the range (model.js), drawn from model.js's
// view model. Each part is drawn again only when its piece of the view model changed.
// docs/themes.md, "Drawing a card"; a theme's own view of this card gets these parts as ctx.std.parts.

import { kit } from '../../js/cards.js';
import { paint } from '../../js/dom.js';
import { hoursChart, legendOf } from './chart.js';

const { el, icon, mark, status } = kit;

// The router gives no figures (or the card could not refresh): the reason, in words, with its icon.
function notice(box, vm) {
  paint(box, [vm.notice, vm.stale], (b) => {
    if (vm.notice) {
      const n = el('p', `trf-notice st st-${vm.notice.level}`);
      n.dataset.item = 'unavailable';
      const word = el('b');
      status(mark(word, 'status'), vm.notice.level, vm.notice.word, { icon: vm.notice.icon });
      n.append(word, ' ', mark(el('span', 'trf-reason', vm.notice.reason), 'reason'));
      b.appendChild(n);
    }
    if (vm.stale) b.appendChild(mark(el('p', 'trf-stale', vm.stale), 'stale'));
  });
}

// What the range covered: the sums, and the busiest hour.
function totals(box, vm) {
  paint(box, vm.totals, (b) => {
    const t = vm.totals;
    if (!t) return;
    b.appendChild(mark(el('p', 'trf-totals', t.text), 'totals'));
    b.appendChild(mark(el('p', 'trf-range', t.range), 'range'));
    if (t.busiest) b.appendChild(mark(el('p', 'trf-busiest', t.busiest.text), 'busiest'));
  });
}

// The hourly chart, its legend and range buttons, and the notes about the gaps. A part: another view can put it in a box of its own.
function chartPart(box) {
  const bar = el('div', 'trf-chart-bar');
  const keys = el('span', 'trf-legend');
  const seg = el('span', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', 'Range');
  bar.append(keys, seg);
  const wrap = el('div', 'chart-box trf-chart-box');
  const canvas = el('canvas');
  canvas.setAttribute('role', 'img');
  wrap.appendChild(canvas);
  const notes = el('div', 'trf-notes');
  box.append(bar, wrap, notes);
  const chart = hoursChart(canvas);
  let keysSig = null;
  return {
    update(c, force) {
      paint(seg, c.modes, () => c.modes.forEach((m) => {
        const b = el('button', null, m.label);
        b.type = 'button';
        b.dataset.action = m.action;
        b.dataset.arg = String(m.arg);
        b.title = m.title;
        b.setAttribute('aria-pressed', String(m.pressed));
        seg.appendChild(b);
      }));
      // the legend: Download, Upload and the kinds of gap there are; drawn again after a theme change (its colors are tokens)
      const kinds = JSON.stringify([...new Set(c.gaps.filter(Boolean))]);
      if (force || kinds !== keysSig) {
        keysSig = kinds;
        keys.replaceChildren(...legendOf(c, canvas).map((k) => {
          const s = el('span', `trf-key${k.pattern ? ' gap' : ''}`);
          const sw = el('i', 'key');
          sw.style.setProperty('--c', k.color);
          sw.setAttribute('aria-hidden', 'true');
          s.append(sw, k.name);
          return s;
        }));
      }
      paint(notes, c.notes, () => c.notes.forEach((n) => notes.appendChild(mark(el('p', 'trf-note', n.text), n.key))));
      notes.hidden = !c.notes.length;
      chart.update(c, force);
    },
    unmount() { chart.unmount(); }
  };
}

// A bar under a row: its length is the share of the biggest in the list.
function bar(pct) {
  const track = el('span', 'trf-bar');
  track.setAttribute('aria-hidden', 'true');
  const fill = el('i');
  fill.style.width = `${pct}%`;
  track.appendChild(fill);
  return track;
}

// One device: a button that opens its dialog. Name and kind over its biggest app, download over upload, and the bar.
function deviceRow(r) {
  const b = el('button', 'trf-row');
  b.type = 'button';
  b.dataset.action = r.open.action;
  b.dataset.arg = r.open.arg;
  b.dataset.arg2 = r.open.arg2;
  b.setAttribute('aria-label', `${r.open.label}: ${r.down} downloaded, ${r.up} uploaded`);
  b.insertAdjacentHTML('afterbegin', icon(r.icon));
  const who = el('span', 'trf-who');
  const name = mark(el('b', 'trf-name', r.name), 'name');
  name.title = r.nameTitle;
  who.appendChild(name);
  if (r.sub) who.appendChild(mark(el('span', 'trf-sub', r.sub), 'kind'));
  if (r.app) {
    const app = mark(el('span', 'trf-app', r.app), 'app');
    app.title = r.appTitle;
    who.appendChild(app);
  }
  const figs = el('span', 'trf-figs');
  const down = mark(el('b', 'trf-down', r.down), 'down');
  down.title = r.share;
  figs.append(down, mark(el('span', 'trf-up', `${r.up} up`), 'up'));
  b.append(who, figs, bar(r.pct));
  return b;
}

export function appRow(r) {
  const li = el('div', 'trf-row app');
  li.append(mark(el('b', 'trf-name', r.name), 'name'));
  const figs = el('span', 'trf-figs');
  figs.append(mark(el('b', 'trf-down', r.down), 'down'), mark(el('span', 'trf-up', `${r.up} up`), 'up'));
  li.append(figs, bar(r.pct));
  return li;
}

// A titled list of rows (the devices, the apps).
function listPart(box, kind, build, help) {
  box.innerHTML = '';
  const h = el('h3', 'rb-t');
  const t = el('span', 'rb-n');
  const sub = el('span', 'muted');
  h.append(t, ' ', sub);
  if (help) h.insertAdjacentHTML('beforeend', `<button type="button" class="info" data-action="toggleHelp" data-help="${help.key}" aria-label="${help.label}">${icon('info')}</button>`);
  const rows = el('div', 'trf-rows');
  const empty = el('p', 'trf-empty');
  const more = el('p', 'trf-more fine');
  box.append(h, rows, empty, more);
  return {
    update(p) {
      kit.text(t, p.title);
      mark(sub, `${kind}Window`);
      kit.text(sub, p.sub);
      kit.list(rows, p.rows, { key: (r) => r.key, render: build });
      empty.hidden = !p.empty;
      kit.text(mark(empty, `${kind}Empty`), p.empty);
      more.hidden = !p.more;
      kit.text(mark(more, `${kind}More`), p.more || '');
    },
    unmount() {}
  };
}

const devices = (box) => listPart(box, 'devices', deviceRow);
const apps = (box) => listPart(box, 'apps', appRow, { key: 'traffic.apps', label: 'About the apps' });

export const parts = { notice, totals, chart: chartPart, devices, apps };

export default {
  parts,
  mount(root) {
    const body = root.querySelector('.trf');
    const area = (cls) => body.appendChild(el('div', cls));
    const stateBox = area('trf-state');
    const noticeBox = area('trf-notices');
    const totalsBox = area('trf-sums');
    const chartBox = area('trf-chart');
    const cols = area('trf-cols');
    const devicesBox = cols.appendChild(el('section', 'trf-block trf-devices'));
    const appsBox = cols.appendChild(el('section', 'trf-block trf-apps'));
    let chart = null;
    const devicePart = devices(devicesBox);
    const appPart = apps(appsBox);
    return {
      update(vm, prev) {
        paint(stateBox, [vm.state, vm.error, vm.empty], (b) => {
          if (vm.state === 'loading') {
            const s = el('div', 'skel-stack');
            ['90%', '60%', '75%'].forEach((w) => { const line = el('div', 'skel line'); line.style.width = w; s.appendChild(line); });
            b.appendChild(s);
          } else if (vm.state === 'error') {
            const e = el('div', 'empty st st-crit');
            e.append(el('b', null, vm.error));
            b.appendChild(e);
          } else if (vm.empty) {
            const e = el('div', 'empty');
            e.innerHTML = icon('chart');
            e.appendChild(mark(el('span', null, vm.empty), 'empty'));
            b.appendChild(e);
          }
        });
        if (vm.state !== 'ready') {
          [noticeBox, totalsBox, chartBox, cols].forEach((b) => { b.hidden = true; b._sig = null; });
          if (chart) { chart.unmount(); chart = null; chartBox.replaceChildren(); }
          return;
        }
        notice(noticeBox, vm);
        totals(totalsBox, vm);
        if (vm.chart) {
          if (!chart) { chartBox.replaceChildren(); chart = chartPart(chartBox); }
          chartBox.hidden = false;
          chart.update(vm.chart, vm === prev);
        } else if (chart) {
          chart.unmount();
          chart = null;
          chartBox.replaceChildren();
          chartBox.hidden = true;
        }
        cols.hidden = !vm.clients;
        if (vm.clients && (!prev || prev.clients !== vm.clients)) devicePart.update(vm.clients);
        if (vm.apps && (!prev || prev.apps !== vm.apps)) appPart.update(vm.apps);
      },
      unmount() { if (chart) chart.unmount(); chart = null; }
    };
  }
};
