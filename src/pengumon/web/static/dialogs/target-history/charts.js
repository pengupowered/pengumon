// The two charts (latency over loss) and what is drawn on them: the clock labels, the dashed marks where a target's
// address changed, the range of the time axis.

import { icon } from '../../js/dom.js';
import { escapeHtml } from '../../js/escape.js';
import { formatClock, localFormat } from '../../js/clock.js';
import { token } from '../../js/chart-theme.js';
import { num } from './figures.js';
import { LIVE, T } from './state.js';

const clock = (ms) => (T.rangeSeconds === LIVE ? formatClock(ms, false, true) : formatClock(ms, T.rangeSeconds > 86400));

// The address changes of the target shown (chart.pgMarks: [{x: ms, text}]): a dashed line each, labeled on the latency
// chart (chart.pgMarkLabels).
const MARKS = {
  id: 'tgtMarks',
  afterDatasetsDraw(chart) {
    const marks = chart.pgMarks || [];
    const { ctx, chartArea: area, scales: { x } } = chart;
    if (!marks.length || !area || !x) return;
    ctx.save();
    ctx.strokeStyle = token('--warn');
    ctx.fillStyle = token('--ink-2');
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.font = '10px system-ui, sans-serif';
    marks.forEach((m) => {
      if (m.x < x.min || m.x > x.max) return;
      const px = x.getPixelForValue(m.x);
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.bottom);
      ctx.stroke();
      if (chart.pgMarkLabels) {
        const w = ctx.measureText('Address changed').width;
        ctx.fillText('Address changed', Math.min(px + 4, area.right - w), area.top + 10);
      }
    });
    ctx.restore();
  }
};

export function makeChart(id, type, yTitle, yExtra) {
  return new Chart(document.getElementById(id), {
    type,
    plugins: [MARKS],
    data: { datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      scales: {
        x: { type: 'linear', grid: { display: false }, border: { color: () => token('--line-strong') }, ticks: { maxTicksLimit: 8, font: { size: 10 }, callback: (v) => clock(v) } },
        y: { beginAtZero: true, afterFit: (scale) => { scale.width = 56; }, ...yExtra, title: { display: true, text: yTitle, font: { size: 10 } }, grid: { color: () => token('--line') }, border: { display: false }, ticks: { font: { size: 10 } } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => !String(item.dataset.id || '').startsWith('band_'),
          callbacks: { title: (items) => (items.length ? clock(items[0].parsed.x) : '') }
        }
      }
    }
  });
}

export function setRange(c, end) {
  c.options.scales.x.min = (end - T.rangeSeconds) * 1000;
  c.options.scales.x.max = end * 1000;
  c.update();
}

// The address changes of a target, as chart marks and a list under the charts; [] clears them.
export function paintMarks(marks) {
  T.latChart.pgMarks = marks;
  T.latChart.pgMarkLabels = true;
  T.lossChart.pgMarks = marks;
  const list = document.getElementById('targetHistEvents');
  list.hidden = !marks.length;
  list.innerHTML = marks.map((m) => `<li>${icon('warn', 'sm')}<span>${escapeHtml(m.text)}</span> <span class="muted">${escapeHtml(localFormat(m.x, { dateStyle: 'medium', timeStyle: 'short' }))}</span></li>`).join('');
}

// A color at about 20% opacity: "#rrggbb" + "33" where it is that, else (an rgb() from a custom theme) color-mix, which the
// canvas takes as well.
export const tint = (color) => (/^#[0-9a-f]{6}$/i.test(color) ? `${color}33` : `color-mix(in srgb, ${color} 20%, transparent)`);

// A bucket's loss as a bar: none where nothing was lost (a 0 bar still drew a faint dash along the axis).
export const lossBar = (v) => (num(v) && v > 0 ? v : null);
