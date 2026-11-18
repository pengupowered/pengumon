// Internet use card, the standard view: the notice when the router gives no traffic figures, the range at the top with its
// download and upload, the line chart (Chart.js, chart.js: a click picks the nearest hour) with its range control and the
// sentences about the hours the router has not published, the strip of the hour (or day) picked on it with the steps to the
// ones before and after, the devices that used the most (a button each: it opens that device's dialog) and the apps
// (model.js), drawn from model.js's view model. Each part is drawn again only when its piece of the view
// model changed.
// docs/themes.md, "Drawing a card"; a theme's own view of this card gets these parts as ctx.std.parts.

import { kit } from '../../js/cards.js';
import { paint } from '../../js/dom.js';
import { hoursChart, legendOf } from './chart.js';
import { fillRangeSelect } from '../../js/ranges.js';

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

// A big figure: "DOWNLOAD", "1.66 GB", "3.7 Mbps on average".
function figure(label, f, key) {
  const box = el('div', 'trf-fig');
  box.dataset.item = key;
  box.appendChild(el('span', 'trf-lbl', label));
  const big = el('span', 'trf-big');
  big.append(mark(el('b', null, f.num), 'num'), ' ', el('small', null, f.unit));
  box.append(big, mark(el('span', 'trf-avg', f.avg), 'avg'));
  return box;
}

// The top of the card: the range, from when to when, and its download and upload as big figures. A part: another view can put
// it in a box of its own.
function totals(box, vm) {
  paint(box, vm.totals, (b) => {
    const t = vm.totals;
    if (!t) return;
    const when = el('div', 'trf-when');
    when.append(mark(el('b', 'trf-range', t.when), 'range'), mark(el('span', null, t.sub), 'rangeSpan'));
    const figs = el('div', 'trf-figs-big');
    figs.append(figure('Download', t.down, 'down'), figure('Upload', t.up, 'up'));
    b.append(when, figs);
  });
}

// The strip under the chart while an hour (or day) is picked on it: when, its download and upload, and the steps ‹ › and
// Whole range (the lists below are its own until then). A part: another view can put it in a box of its own.
function pick(box, vm) {
  const had = box.contains(document.activeElement) ? document.activeElement : null; // a step button keeps the focus across the redraw
  const again = had && had.dataset.action ? `[data-action="${had.dataset.action}"]${had.dataset.arg ? `[data-arg="${had.dataset.arg}"]` : ''}` : null;
  box.hidden = !vm.pick;
  paint(box, vm.pick, (b) => {
    const p = vm.pick;
    if (!p) return;
    const what = el('span', 'trf-strip-what');
    what.append(mark(el('b', 'trf-strip-when', p.when), 'when'), ' ', mark(el('span', 'trf-strip-figs', [p.down, p.up].filter(Boolean).join(' · ')), 'figs'));
    if (p.avg) what.append(' ', mark(el('span', 'trf-strip-avg', p.avg), 'avg'));
    const steps = el('span', 'trf-steps');
    const step = (label, action, arg, on, text) => {
      const s = el('button', 'btn sm outline', text);
      s.type = 'button';
      s.dataset.action = action;
      if (arg) s.dataset.arg = arg;
      s.disabled = !on;
      s.setAttribute('aria-label', label);
      return s;
    };
    const unit = p.steps.unit === 'day' ? 'day' : 'hour';
    steps.append(step(`The ${unit} before`, 'trafficStep', 'prev', p.steps.prev, '‹'), step(`The ${unit} after`, 'trafficStep', 'next', p.steps.next, '›'),
      step('Back to the whole range: its devices and apps in the lists', 'trafficWholeRange', null, true, '✕ Whole range'));
    b.append(what, steps);
  });
  if (again && !box.contains(document.activeElement)) {
    const target = box.querySelector(again);
    (target && !target.disabled ? target : box.querySelector('.trf-steps button:not([disabled])'))?.focus({ preventScroll: true });
  }
}

// The line chart, its legend and range control, and the notes about the gaps; Last hour has one point, so a sentence instead of
// the chart. A click on the chart picks the hour or day nearest it (chart.js puts it on the canvas for the trafficPick action).
// A part: another view can put it in a box of its own.
function chartPart(box) {
  const bar = el('div', 'trf-chart-bar');
  const keys = el('span', 'trf-legend');
  const seg = el('span', 'range-host');
  bar.append(keys, seg);
  const wrap = el('div', 'chart-box trf-chart-box');
  const canvas = el('canvas');
  canvas.setAttribute('role', 'img');
  canvas.dataset.action = 'trafficPick';
  wrap.appendChild(canvas);
  const single = mark(el('p', 'trf-single', 'One hour is one point, so there is no line to draw: the figures above are that hour. Choose 24 hours or longer to see the hours before it and pick one.'), 'single');
  const notes = el('div', 'trf-notes');
  box.append(bar, wrap, single, notes);
  const chart = hoursChart(canvas, { pickable: true });
  let keysSig = null;
  return {
    update(c, force) {
      const pressed = c.modes.find((m) => m.pressed);
      fillRangeSelect(seg, { options: c.modes.map((m) => ({ id: m.arg, words: m.label })), value: pressed ? pressed.arg : c.modes[0].arg, action: 'trafficRange', label: 'Range' });
      wrap.hidden = c.single;
      single.hidden = !c.single;
      keys.style.visibility = c.single ? 'hidden' : '';
      // the legend: Download, Upload and the kinds of gap there are; drawn again after a theme change (its colors are tokens)
      const kinds = JSON.stringify([...new Set(c.gaps.filter(Boolean))]);
      if (force || kinds !== keysSig) {
        keysSig = kinds;
        keys.replaceChildren(...legendOf(c, canvas).map((k) => {
          const s = el('span', `trf-key${k.pattern ? ' gap' : ''}`);
          const sw = el('i', k.line ? 'key line' : 'key');
          sw.style.setProperty('--c', k.color);
          sw.setAttribute('aria-hidden', 'true');
          s.append(sw, k.name);
          return s;
        }));
      }
      paint(notes, c.notes, () => c.notes.forEach((n) => notes.appendChild(mark(el('p', 'trf-note', n.text), n.key))));
      notes.hidden = !c.notes.length || c.single;
      if (!c.single) chart.update(c, force);
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
  const li = el('div', `trf-row app${r.muted ? ' rest' : ''}`);
  if (r.title) li.title = r.title;
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

export const parts = { notice, pick, totals, chart: chartPart, devices, apps };

export default {
  parts,
  mount(root) {
    const body = root.querySelector('.trf');
    const area = (cls) => body.appendChild(el('div', cls));
    const stateBox = area('trf-state');
    const noticeBox = area('trf-notices');
    const totalsBox = area('trf-sums');
    const chartBox = area('trf-chart');
    const pickBox = area('trf-pick');
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
          [noticeBox, pickBox, totalsBox, chartBox, cols].forEach((b) => { b.hidden = true; b._sig = null; });
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
        pick(pickBox, vm);
        cols.hidden = !vm.clients;
        if (vm.clients && (!prev || prev.clients !== vm.clients)) devicePart.update(vm.clients);
        if (vm.apps && (!prev || prev.apps !== vm.apps)) appPart.update(vm.apps);
      },
      unmount() { if (chart) chart.unmount(); chart = null; }
    };
  }
};
