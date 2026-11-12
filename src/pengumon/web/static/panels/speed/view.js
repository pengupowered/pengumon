// Speed card, the standard view: the last result (download and upload against the plan, when, from where, the delay under
// load), the history chart (Chart.js), the next test and the month's data use, and Run now with its confirmation, drawn
// from model.js's view model. Each part is drawn again only when its piece of the view model changed.
// docs/themes.md, "Drawing a card"; a theme's own view of this card gets these parts as ctx.std.parts.

import { kit } from '../../js/cards.js';
import { paint } from '../../js/dom.js';
import { series, token } from '../../js/chart-theme.js';
import { localFormat } from '../../js/clock.js';

const { el, icon, mark, status } = kit;

// A status as icon + word in a new element (data-st on it).
function badge(s, cls) {
  const node = el('span', cls);
  node.dataset.role = 'status';
  return status(node, s.level, s.word, { icon: s.icon });
}

// Download and upload: the figure big, the share of the plan under it.
function figure(label, f, key) {
  const box = el('div', 'spd-fig');
  box.dataset.item = key;
  box.appendChild(el('span', 'spd-lbl', label));
  const big = el('span', 'spd-big');
  big.append(mark(el('b', null, f.num), 'num'), ' ', el('small', null, f.unit));
  box.appendChild(big);
  if (f.plan) box.appendChild(mark(el('span', 'spd-plan', f.plan), 'plan'));
  if (f.own) box.appendChild(mark(el('span', 'spd-own', f.own), 'own'));
  return box;
}

// The last result: the figures, when and from where, why it is not whole, the remark, the delay grade.
function last(box, vm) {
  paint(box, vm.last, (b) => {
    const l = vm.last;
    if (!l) return;
    const figs = el('div', 'spd-figs');
    figs.append(figure('Download', l.down, 'down'), figure('Upload', l.up, 'up'));
    b.appendChild(figs);
    if (l.by) {
      const by = mark(el('p', 'spd-by', l.by.text), 'by');
      by.title = l.by.title;
      b.appendChild(by);
    }
    const meta = el('p', 'spd-meta');
    const when = mark(el('span', null, l.when.text), 'when');
    when.title = l.when.title;
    meta.append(when, ' · ', mark(el('span', null, l.server), 'server'));
    b.appendChild(meta);
    if (l.status) {
      const why = el('p', `spd-why st st-${l.status.level}`);
      why.append(badge(l.status, 'spd-st'));
      if (l.status.reason) why.append(' ', mark(el('span', null, l.status.reason), 'reason'));
      b.appendChild(why);
    }
    if (l.traffic) b.appendChild(mark(el('p', 'spd-remark', l.traffic), 'traffic'));
    if (l.note) b.appendChild(mark(el('p', 'spd-remark', l.note), 'remark'));
    if (l.bufferbloat) {
      const bb = el('p', 'spd-bloat');
      bb.dataset.item = 'bufferbloat';
      bb.append(el('span', 'spd-lbl', 'Bufferbloat'), ' ');
      const grade = badge(l.bufferbloat.status, `badge quiet st-${l.bufferbloat.status.level}`);
      grade.title = l.bufferbloat.title;
      bb.append(grade, ' ', mark(el('span', null, l.bufferbloat.text), 'delay'));
      b.appendChild(bb);
    }
  });
}

// The newest test measured nothing: what happened.
function problem(box, vm) {
  paint(box, vm.problem, (b) => {
    const p = vm.problem;
    if (!p) return;
    const row = el('div', `spd-problem st st-${p.status.level}`);
    row.dataset.item = 'problem';
    const when = mark(el('span', 'spd-when', p.when.text), 'when');
    when.title = p.when.title;
    row.append(badge(p.status, 'spd-st'), ' ', when, ' ', mark(el('span', 'spd-reason', p.reason), 'reason'));
    b.appendChild(row);
  });
}

// The chart's line colors and mark shapes (tokens, read at drawing time: a theme change draws it again).
function datasets(c, canvas) {
  const down = series(0, canvas);
  const up = series(1, canvas);
  const plan = token('--ink-3', canvas);
  const bad = token('--crit', canvas);
  const quiet = token('--ink-3', canvas);
  const line = (label, color, points) => ({ label, data: points, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: points.length < 40 ? 3 : 0, pointHoverRadius: 5, tension: 0, ownCurve: true });
  // the plan: a dashed line (a hex color gets an alpha, which a theme's "fill" style takes as not its to fill)
  const dashed = (label, value) => ({ label, data: [{ x: c.xMin, y: value }, { x: c.xMax, y: value }], borderColor: /^#[0-9a-f]{6}$/i.test(plan) ? `${plan}e6` : plan, backgroundColor: 'transparent', borderWidth: 1, borderDash: [5, 4], pointRadius: 0, pointHoverRadius: 0, tension: 0, isPlan: true });
  // failed and skipped tests: shapes on the axis (a cross, a hollow circle), so color is not the only difference
  const marks = (label, points, color, style) => ({ label, data: points, showLine: false, borderWidth: 0, pointStyle: style, pointRadius: 6, pointHoverRadius: 8, pointBorderWidth: 2, pointBorderColor: color, pointBackgroundColor: 'transparent', clip: false, isMark: true });
  const list = [];
  if (c.planDown) list.push(dashed('Download plan', c.planDown));
  if (c.planUp) list.push(dashed('Upload plan', c.planUp));
  if (c.down.length) list.push(line('Download', down, c.down));
  if (c.up.length) list.push(line('Upload', up, c.up));
  if (c.failed.length) list.push(marks('Failed', c.failed, bad, 'crossRot'));
  if (c.skipped.length) list.push(marks('Skipped', c.skipped, quiet, 'circle'));
  return list;
}

function legend(c, canvas) {
  const out = [];
  const key = (name, color, cls, style) => out.push({ name, color, cls, style });
  if (c.down.length) key('Download', series(0, canvas), 'line');
  if (c.up.length) key('Upload', series(1, canvas), 'line');
  if (c.planDown || c.planUp) key('Plan', token('--ink-3', canvas), 'line dashed');
  if (c.failed.length) key('Failed', token('--crit', canvas), 'mark', '×');
  if (c.skipped.length) key('Skipped', token('--ink-3', canvas), 'mark', '○');
  return out;
}

// The history chart and its range buttons. A part: another view can put it in a box of its own.
function chartPart(box) {
  const bar = el('div', 'spd-chart-bar');
  const seg = el('span', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', 'History range');
  const keys = el('span', 'spd-legend');
  bar.append(keys, seg);
  const wrap = el('div', 'chart-box spd-chart-box');
  const canvas = el('canvas');
  canvas.setAttribute('role', 'img');
  wrap.appendChild(canvas);
  box.append(bar, wrap);
  let chart = null;
  let shown = null;
  let cur = null; // the chart's data now: the axis reads its ends from here

  function options() {
    return {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      layout: { padding: { right: 8, top: 8, bottom: 2 } },
      scales: {
        x: {
          type: 'linear', min: cur.xMin, max: cur.xMax, grid: { display: false },
          afterBuildTicks(scale) {
            const step = (cur.days <= 7 ? 1 : 5) * 86400000;
            const ticks = [];
            for (let v = cur.xMax; v >= cur.xMin; v -= step) ticks.unshift({ value: v });
            scale.ticks = ticks;
          },
          ticks: { font: { size: 10 }, maxRotation: 0, callback: (v) => localFormat(v, { month: 'short', day: 'numeric' }) }
        },
        y: { beginAtZero: true, grace: '8%', ticks: { font: { size: 10 }, maxTicksLimit: 5, callback: (v) => `${v}` }, title: { display: true, text: 'Mbps', font: { size: 10 } } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => !item.dataset.isPlan,
          callbacks: {
            title: (items) => localFormat(items[0].raw.x, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
            label: (item) => (item.raw.tip ? item.raw.tip : `${item.dataset.label}: ${item.raw.y} Mbps`)
          }
        }
      }
    };
  }

  return {
    update(c, force) {
      const sig = JSON.stringify(c);
      if (chart && sig === shown && !force) return; // nothing changed: a hover stays
      shown = sig;
      paint(seg, c.modes, () => c.modes.forEach((m) => {
        const b = el('button', null, m.label);
        b.type = 'button';
        b.dataset.action = m.action;
        b.dataset.arg = String(m.arg);
        b.title = m.title;
        b.setAttribute('aria-pressed', String(m.pressed));
        seg.appendChild(b);
      }));
      keys.replaceChildren(...legend(c, canvas).map((k) => {
        const s = el('span', 'spd-key');
        const sw = k.style ? el('i', 'spd-shape', k.style) : el('i', `key ${k.cls}`);
        sw.style.setProperty('--c', k.color);
        sw.setAttribute('aria-hidden', 'true');
        s.append(sw, k.name);
        return s;
      }));
      canvas.setAttribute('aria-label', c.label);
      cur = c;
      const sets = datasets(c, canvas);
      if (!chart) {
        chart = new Chart(canvas, { type: 'line', data: { datasets: sets }, options: options() });
        return;
      }
      chart.data.datasets = sets;
      chart.options.scales.x.min = c.xMin;
      chart.options.scales.x.max = c.xMax;
      chart.update('none');
    },
    unmount() { if (chart) chart.destroy(); chart = null; }
  };
}

// Run now: the button, the question that asks first (it uses data), progress, and why a test did not start.
function run(box, vm) {
  paint(box, { run: vm.run, confirm: vm.confirm, progress: vm.progress, note: vm.note }, (b) => {
    const btn = kit.action(vm.run, { className: 'btn sm outline spd-run-btn' });
    btn.disabled = vm.run.disabled;
    btn.title = vm.run.title;
    b.appendChild(btn);
    if (vm.confirm) {
      const q = el('div', 'spd-confirm');
      q.setAttribute('role', 'alertdialog');
      q.setAttribute('aria-label', vm.confirm.title);
      q.dataset.item = 'confirm';
      q.append(mark(el('b', null, vm.confirm.title), 'question'), mark(el('span', null, vm.confirm.text), 'data'));
      const row = el('div', 'spd-confirm-btns');
      row.append(kit.action(vm.confirm.no, { className: 'btn sm' }), kit.action(vm.confirm.yes, { className: 'btn sm primary' }));
      q.appendChild(row);
      b.appendChild(q);
    }
    if (vm.progress) {
      const p = el('p', 'spd-progress');
      p.setAttribute('role', 'status');
      p.tabIndex = -1;
      p.insertAdjacentHTML('beforeend', `${icon('refresh', 'spin')} `);
      p.append(mark(el('b', null, vm.progress.text), 'progress'), ' ', mark(el('span', 'spd-detail', vm.progress.detail), 'progressDetail'));
      b.appendChild(p);
    }
    if (vm.note) b.appendChild(mark(el('p', `spd-notice st st-${vm.note.level}`, vm.note.text), 'notice'));
  });
}

const lines = {
  next(box, vm) {
    paint(box, [vm.next, vm.off, vm.usage, vm.usageLevel], (b) => {
      if (vm.off) {
        const p = el('p', 'spd-line');
        p.append(mark(el('span', null, vm.off.text), 'off'), ' ', kit.action(vm.off.link, { className: 'spd-link' }));
        b.appendChild(p);
      } else if (vm.next) {
        b.appendChild(mark(el('p', 'spd-line', vm.next), 'next'));
      }
      const u = mark(el('p', `spd-line${vm.usageLevel ? ` st st-${vm.usageLevel}` : ''}`, vm.usage), 'usage');
      b.appendChild(u);
    });
  }
};

export const parts = { figure, last, problem, chart: chartPart, run, lines };

export default {
  parts,
  mount(root) {
    const body = root.querySelector('.spd');
    const area = (cls) => body.appendChild(el('div', cls));
    const stateBox = area('spd-state');
    const problemBox = area('spd-problems');
    const lastBox = area('spd-last');
    const chartBox = area('spd-chart');
    const lineBox = area('spd-lines');
    const runBox = area('spd-run');
    let chart = null;
    return {
      update(vm, prev) {
        const had = root.contains(document.activeElement);
        paint(stateBox, [vm.state, vm.error, vm.empty, vm.off], (b) => {
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
            e.innerHTML = icon('gauge');
            e.appendChild(mark(el('span', null, vm.empty), 'empty'));
            b.appendChild(e);
          }
        });
        if (vm.state !== 'ready') {
          [problemBox, lastBox, chartBox, lineBox, runBox].forEach((b) => { b.hidden = true; b._sig = null; });
          if (chart) { chart.unmount(); chart = null; chartBox._sig = null; chartBox.replaceChildren(); }
          return;
        }
        problem(problemBox, vm);
        last(lastBox, vm);
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
        lines.next(lineBox, vm);
        // the focus moves with the question: into it when it opens, back to the button (or the progress) when it closes
        const closed = prev && prev.confirm && !vm.confirm;
        run(runBox, vm);
        if (had && closed) (runBox.querySelector('.spd-progress') || runBox.querySelector('button')).focus();
        else if (had && vm.confirm && !(prev && prev.confirm)) runBox.querySelector('.spd-confirm .primary')?.focus();
      },
      unmount() { if (chart) chart.unmount(); chart = null; }
    };
  }
};
