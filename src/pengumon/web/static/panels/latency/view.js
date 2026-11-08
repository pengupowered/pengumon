// The latency panel's standard view: the legend, the Path / Each target switch, the note, a latency chart and a thin loss
// lane under it, drawn from charts.js's view model. Chart.js, with text and grid colors from the theme
// (chart-theme.js) and series colors from the tokens (a card's own colors apply). docs/themes.md, "Drawing a card"; a
// theme's own view of this card gets these parts as ctx.std.parts (`charts` draws both charts into two canvases).

import { kit } from '../../js/cards.js';
import { token } from '../../js/chart-theme.js';
import { formatMs } from '../../js/util.js';

const { escapeHtml } = kit;
const has = (v) => v !== null && v !== undefined;
const fmtMs = (v) => formatMs(v, { unit: true, empty: 'no reply', tenths: true });

// Index of the point nearest to `ts` (times are sorted).
function nearest(times, ts) {
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < ts) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// Shaded incident windows behind the lines, and each series' name at its line end. `vm()` is the view model now.
function pathPlugin(vm) {
  return {
    id: 'pathDecor',
    beforeDatasetsDraw(chart) {
      if (!vm()) return; // drawn once when made, before the first view model
      const { times, incidents } = vm();
      if (!times.length || !incidents.length) return;
      const { ctx, chartArea: a, scales: { x } } = chart;
      const t0 = times[0];
      const t1 = times[times.length - 1];
      incidents.forEach((inc) => {
        if (inc.end < t0 || inc.start > t1) return;
        const x0 = x.getPixelForValue(nearest(times, Math.max(inc.start, t0)));
        const x1 = x.getPixelForValue(nearest(times, Math.min(inc.end, t1)));
        const color = token(inc.level === 'crit' ? '--crit' : inc.level === 'busy' ? '--busy' : '--warn');
        ctx.save();
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = color;
        ctx.fillRect(x0, a.top, Math.max(2, x1 - x0), a.bottom - a.top);
        ctx.globalAlpha = 0.8;
        ctx.strokeStyle = color;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x0 + 0.5, a.top);
        ctx.lineTo(x0 + 0.5, a.bottom);
        ctx.stroke();
        ctx.restore();
      });
    },
    afterDatasetsDraw(chart) {
      const { ctx, chartArea: a } = chart;
      if (chart.width < 480) return; // a phone: the legend names the lines, the right margin goes to the chart
      const ends = [];
      chart.data.datasets.forEach((d, i) => {
        if (!d.pathLabel || !chart.isDatasetVisible(i)) return;
        const meta = chart.getDatasetMeta(i);
        let k = d.data.length - 1;
        while (k >= 0 && !has(d.data[k])) k--;
        if (k < 0 || !meta.data[k]) return;
        ends.push({ label: d.pathLabel, color: d.borderColor, x: meta.data[k].x, y: meta.data[k].y, y0: meta.data[k].y });
      });
      ends.sort((p, q) => p.y - q.y);
      // spread the names at least 14 px apart: downwards from the top one, then back up from the bottom edge, so a group of
      // lines that coincide near the floor of the chart (a healthy line) does not push the last name over the time axis
      for (let k = 1; k < ends.length; k++) if (ends[k].y - ends[k - 1].y < 14) ends[k].y = ends[k - 1].y + 14;
      const floor = a.bottom - 7;
      if (ends.length && ends[ends.length - 1].y > floor) {
        ends[ends.length - 1].y = floor;
        for (let k = ends.length - 2; k >= 0 && ends[k].y > ends[k + 1].y - 14; k--) ends[k].y = ends[k + 1].y - 14;
      }
      const load = chart.scales.yLoad;
      const labelX = (load && load.options.display ? load.right : a.right) + 8;
      ctx.save();
      ctx.font = `600 11px ${token('--font')}`;
      ctx.textBaseline = 'middle';
      ends.forEach((e) => {
        ctx.fillStyle = e.color;
        ctx.strokeStyle = token('--surface-1');
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(e.x, e.y0, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = token('--ink-2');
        ctx.fillText(e.label, labelX, e.y);
      });
      ctx.restore();
    }
  };
}

// An empty loss lane says so, instead of looking like a chart that failed to load.
function quietLane(vm) {
  return {
    id: 'quietLane',
    afterDraw(chart) {
      if (!vm()) return;
      const { times, loss } = vm();
      if (!times.length || loss.some((v) => v > 0)) return;
      const { ctx, chartArea: a } = chart;
      ctx.save();
      ctx.font = `11px ${token('--font')}`;
      ctx.fillStyle = token('--ink-3');
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('no lost probes in this window', (a.left + a.right) / 2, (a.top + a.bottom) / 2);
      ctx.restore();
    }
  };
}

// The datasets of the latency chart. `at`: an element of the card (its own colors, see chart-theme.js token).
function datasets(m, at) {
  const sets = [];
  const resolve = (c) => (c.token ? token(c.token, at) : c.value);
  m.lines.forEach((l) => {
    const color = resolve(l.color);
    if (l.kind === 'target') {
      sets.push({ id: l.id, target: l.target, label: l.label, data: l.data, borderColor: color, backgroundColor: color,
        borderWidth: 1.5, pointRadius: 0, tension: 0.25, spanGaps: false, order: 1 });
      return;
    }
    if (l.id === 'path_internet' && m.band) {
      sets.push({ id: 'band_lo', label: 'p10', data: m.band.lo, borderWidth: 0, pointRadius: 0, fill: false, spanGaps: false, order: 5 });
      sets.push({ id: 'band_hi', label: 'p10–p90', data: m.band.hi, borderWidth: 0, pointRadius: 0, backgroundColor: `${resolve(m.band.color)}26`, fill: '-1', spanGaps: false, order: 5 });
    }
    sets.push({ id: l.id, label: l.label, pathLabel: l.label, data: l.data, borderColor: color, backgroundColor: color,
      borderWidth: 2, pointRadius: 0, tension: 0.25, spanGaps: false, order: 1, ...(l.dash ? { borderDash: l.dash } : {}) });
  });
  if (m.wan) {
    sets.push({ id: 'wan_load', label: 'WAN download', data: m.wan.data, yAxisID: 'yLoad', borderColor: token('--ink-4'),
      backgroundColor: `${token('--ink-4')}1f`, fill: 'origin', borderWidth: 1, borderDash: [4, 3], pointRadius: 0, spanGaps: true, tension: 0.2, order: 10 });
  }
  return sets;
}

// How many time labels fit across a chart this wide.
const timeTicks = (width) => (width < 480 ? 3 : width < 800 ? 4 : 6);

// The two charts in two canvases: the latency and the loss lane under it, whose plot area starts and ends where the
// latency chart's does (its right axis and line-end names included), so a lost probe sits under the moment it belongs to.
function charts(latencyCanvas, lossCanvas) {
  let m = null;
  const vm = () => m;
  let lat = null;
  let lost = null;
  const alignLossLane = () => {
    if (!lat.chartArea) return;
    lost.options.layout.padding.right = Math.max(0, lat.width - lat.chartArea.right);
  };
  lat = new Chart(latencyCanvas, {
    type: 'line',
    data: { labels: [], datasets: [] },
    plugins: [pathPlugin(vm)],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { right: window.innerWidth < 520 ? 0 : 70, top: 6 } },
      interaction: { mode: 'index', intersect: false },
      // fewer time labels on a narrow chart (a phone): six "HH:MM:SS" labels do not fit in ~200 px; no line-end names
      onResize: (chart, size) => {
        chart.options.scales.x.ticks.maxTicksLimit = timeTicks(size.width);
        chart.options.layout.padding.right = size.width < 480 ? 0 : 70;
        if (lost) requestAnimationFrame(() => { if (!lost) return; alignLossLane(); lost.update('none'); });
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: timeTicks(window.innerWidth), maxRotation: 0, autoSkipPadding: 16 } },
        // a hard floor: one bad reading must not move 0 ms to mid-chart
        y: { min: 0, suggestedMax: 30, ticks: { callback: (v) => `${v} ms`, maxTicksLimit: 5 }, border: { display: false } },
        yLoad: { position: 'right', min: 0, suggestedMax: 100, grid: { drawOnChartArea: false }, display: false,
          ticks: { callback: (v) => `${v} Mbps`, maxTicksLimit: 4 }, border: { display: false } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          // the p10-p90 band has no reading of its own; a WAN gap (no router reading near that moment) is left out too
          filter: (item) => !String(item.dataset.id).startsWith('band_') && !(item.dataset.id === 'wan_load' && !has(item.parsed.y)),
          callbacks: {
            label: (c) => (c.dataset.id === 'wan_load' ? ` WAN download: ${c.parsed.y} Mbps`
              : ` ${c.dataset.label}: ${fmtMs(c.parsed.y)}`),
            afterLabel: (c) => (m && String(c.dataset.id).startsWith('path_') ? m.memberLines(c.dataset.id, c.dataIndex) : [])
          }
        }
      }
    }
  });
  lost = new Chart(lossCanvas, {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { right: 70 } },
      scales: {
        x: { display: false },
        y: { min: 0, max: 100, afterFit: (axis) => { axis.width = Math.max(axis.width, lat?.scales.y?.width ?? 0); },
          ticks: { stepSize: 100, callback: (v) => (v === 100 ? 'lost' : '') }, grid: { drawOnChartArea: true }, border: { display: false } }
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => (m ? m.lossLabel(c.dataIndex, c.parsed.y) : '') } } }
    },
    plugins: [quietLane(vm)]
  });
  return {
    update(next) {
      m = next;
      lat.data.labels = m.labels;
      lat.data.datasets = datasets(m, latencyCanvas);
      lat.options.scales.yLoad.display = !!(m.wan && m.wan.axis);
      lost.data.labels = m.labels;
      lost.data.datasets = [{ id: 'loss', label: 'Lost', data: m.loss.map((v) => (v > 0 ? v : null)), backgroundColor: token('--crit'),
        borderWidth: 0, barPercentage: 0.9, categoryPercentage: 1, minBarLength: 3 }];
      lat.update('none');
      alignLossLane();
      lost.update('none');
    },
    unmount() {
      lat.destroy();
      lost.destroy();
      lat = null;
      lost = null;
    }
  };
}

// The legend: what each line is (item marks: label, sub), as markup.
const KEY_STYLE = { line: '', band: ';opacity:.3', wan: ';opacity:.5', lost: ';width:3px;height:11px' };
function legendHtml(items) {
  return items.map((it) => {
    const key = `<i class="key${it.kind === 'line' ? ' line' : ''}${it.dash ? ' dash' : ''}" style="--c:${escapeHtml(it.color)}${KEY_STYLE[it.kind]}"></i>`;
    const words = it.kind === 'line'
      ? `<b data-v="label">${escapeHtml(it.label)}</b>${it.sub !== undefined ? `<span class="muted" data-v="sub">${escapeHtml(it.sub)}</span>` : ''}`
      : `<span data-v="label">${escapeHtml(it.label)}</span>`;
    return `<span class="lg" data-item="${escapeHtml(it.key)}"${it.title !== undefined ? ` title="${escapeHtml(it.title)}"` : ''}>${key}${words}</span>`;
  }).join('');
}

function legend(box, note) {
  note.dataset.v = 'note';
  return {
    update(m) {
      kit.html(box, legendHtml(m.legend));
      kit.text(note, m.note);
    }
  };
}

// The legend laid out a theme's own way (a part): a keyed list of entries in `box`, one per legend item. Each entry's
// parts are made here, `arrange(parts, item)` puts them in an element of the theme's and returns it: `key` (i.key, in
// the item's color: --c is set on the entry), `label` (b.<p>-lg-n), and for a line `now` (its newest reading,
// .<p>-lg-now) and `sub` (what it is made of, .<p>-lg-s, hidden when it has none). The entry gets the item's title.
function legendList(box, prefix, arrange) {
  const { el, mark, text } = kit;
  const refs = new WeakMap();
  const render = (it) => {
    const p = { key: el('i', it.kind === 'line' ? 'key line' : 'key'), label: mark(el('b', `${prefix}-lg-n`), 'label') };
    if (it.kind === 'line') {
      p.now = el('span', `${prefix}-lg-now`);
      p.sub = mark(el('span', `${prefix}-lg-s`), 'sub');
    }
    const entry = arrange(p, it);
    refs.set(entry, p);
    return entry;
  };
  return {
    update(m) {
      kit.list(box, m.legend, { key: (it) => it.key, render, sig: (it) => it.kind });
      m.legend.forEach((it, i) => {
        const entry = box.children[i];
        const p = refs.get(entry);
        entry.style.setProperty('--c', it.color);
        p.key.classList.toggle('dash', !!it.dash);
        if (it.title !== undefined) entry.title = it.title;
        text(p.label, it.label);
        if (!p.sub) return;
        p.sub.hidden = it.sub === undefined;
        text(p.sub, it.sub);
        text(p.now, it.now);
      });
    }
  };
}

// The Path / Each target switch: its buttons are panel.html's, or made here in a group of a view that has none.
function modes(group) {
  return {
    update(m) {
      if (!group.children.length) {
        group.replaceChildren(...m.modes.map((b) => {
          const button = kit.el('button', null, b.label);
          button.type = 'button';
          button.dataset.action = b.action;
          button.dataset.arg = b.arg;
          button.title = b.title;
          return button;
        }));
      }
      m.modes.forEach((b) => {
        const button = group.querySelector(`[data-arg="${b.arg}"]`);
        if (!button) return;
        button.classList.toggle('active', b.pressed);
        button.setAttribute('aria-pressed', String(b.pressed));
      });
    }
  };
}

// Both charts in two chart boxes of their own, appended to `box` (a view without the body's canvases): the latency chart
// and the loss lane, as panel.html has them. Returns the charts part.
function plot(box) {
  const canvas = (className, height, label) => {
    const chartBox = box.appendChild(kit.el('div', `chart-box ${className}`));
    chartBox.style.setProperty('--h', height);
    const c = chartBox.appendChild(kit.el('canvas'));
    c.setAttribute('role', 'img');
    c.setAttribute('aria-label', label);
    return c;
  };
  return charts(canvas('lat-main', '250px', 'Latency over time per target'), canvas('lat-loss', '44px', 'Packet loss over time, worst target'));
}

export const parts = { charts, plot, legend, legendHtml, legendList, modes, datasets };

export default {
  parts,
  mount(root) {
    const p = [
      legend(root.querySelector('.lat-legend'), root.querySelector('.lat-note')),
      modes(root.querySelector('.lat-view')),
      charts(root.querySelector('.lat-main canvas'), root.querySelector('.lat-loss canvas'))
    ];
    return {
      // also after a theme change (same view model): the series colors are read again
      update(m) { p[1].update(m); p[2].update(m); p[0].update(m); },
      unmount() { p[2].unmount(); }
    };
  }
};
