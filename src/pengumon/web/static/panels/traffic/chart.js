// The traffic chart (Chart.js lines, drawn as the Speed card's history is): download and upload per clock hour, or per day
// (the card past 90 days, the device dialog past 7 days), used by the card (view.js) and by the device dialog (client.js). It
// draws plain data: {hours, ends, titles, down, up, gaps, unit, label, picked}, one entry per hour or day, where `hours[i]`
// and `ends[i]` are the epoch seconds the hour or day begins and ends (a day the range cuts is shorter), `down` and `up` are
// bytes or null for one with no figure, `gaps[i]` says why: 'unpublished' (the router has not published the hour yet) or
// 'nodata' (never stored), `unit` is 'hour' or 'day' (the y axis says "per hour" or "per day") and `picked` is the index of
// the point the card shows at its top (a dashed line and a bigger point; null: none). A point sits in the middle of its hour
// or day. A gap breaks the line and is a hatched band the height of the chart, never a point at 0: a router that has not
// told is not a router that moved nothing. Colors are tokens, read when it draws (a theme change draws it again).
//
// hoursChart(canvas, { pickable: true }) (the card): a click on the chart puts the start and end of the nearest point on the canvas
// (data-arg, data-arg2; empty when there is none) before the click reaches the page's actions (js/dispatch.js), so the action the
// view names on the canvas (data-action) reads the hour or day picked. Chart.js' own click handler runs a frame later, too late.

import { series, token } from '../../js/chart-theme.js';
import { formatBytes } from '../../js/format.js';
import { axisLabel, monthTicks, monthsPast } from '../../js/ranges.js';

const HEX6 = /^#[0-9a-f]{6}$/i;
// The two kinds of gap: its words and the token its hatching is drawn in. The newest hour not being published yet is the
// one a person looks for; a stretch that was never stored is quieter.
const GAPS = { unpublished: ['Not published yet', '--ink-3'], nodata: ['Not stored', '--line-strong'] };

// The upload line is the quieter of the two: the download's neighbor color a little transparent (a color that is not a plain
// hex stays as it is).
const quieter = (color) => (HEX6.test(color) ? `${color}b3` : color);

// Diagonal hatching in the ink color, as a canvas pattern: what a gap is drawn with (color is not the only difference).
function hatch(canvas, tokenName) {
  const tile = document.createElement('canvas');
  tile.width = 8;
  tile.height = 8;
  const g = tile.getContext('2d');
  g.strokeStyle = token(tokenName, canvas);
  g.lineWidth = 1;
  g.beginPath();
  g.moveTo(0, 8); g.lineTo(8, 0);
  g.moveTo(-2, 2); g.lineTo(2, -2);
  g.moveTo(6, 10); g.lineTo(10, 6);
  g.stroke();
  return canvas.getContext('2d').createPattern(tile, 'repeat');
}

// The chart's legend entries for the data: [{name, color, line, pattern}] (the hatched ones are drawn as hatching by the view).
export function legendOf(c, canvas) {
  const out = [{ name: 'Download', color: series(0, canvas), line: true }, { name: 'Upload', color: quieter(series(1, canvas)), line: true }];
  Object.entries(GAPS).forEach(([kind, [name, tone]]) => { if (c.gaps.includes(kind)) out.push({ name, color: token(tone, canvas), pattern: true }); });
  return out;
}

// The middle of entry i, in ms (the x of its point).
const middle = (c, i) => ((c.hours[i] + c.ends[i]) / 2) * 1000;

function datasets(c, canvas) {
  const few = c.hours.length < 40;
  const radius = c.hours.map((_, i) => (i === c.picked ? 5 : few ? 3 : 0));
  const line = (label, color, values) => ({
    label, data: values.map((v, i) => ({ x: middle(c, i), y: v })), borderColor: color, backgroundColor: color, borderWidth: 2,
    pointRadius: radius, pointHoverRadius: 5, tension: 0, spanGaps: false
  });
  return [line('Download', series(0, canvas), c.down), line('Upload', quieter(series(1, canvas)), c.up)];
}

// The gaps as hatched bands behind the lines, and the picked point's dashed line, drawn by a plugin from the chart's own
// options (so a redraw after a resize draws them again).
const marks = {
  id: 'trafficMarks',
  beforeDatasetsDraw(chart) {
    const o = chart.options.plugins.trafficMarks;
    if (!o || !o.data) return;
    const { data: c, patterns } = o;
    const { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save();
    c.gaps.forEach((g, i) => {
      if (!g || !patterns[g]) return;
      const left = Math.max(a.left, x.getPixelForValue(c.hours[i] * 1000));
      const right = Math.min(a.right, x.getPixelForValue(c.ends[i] * 1000));
      if (right <= left) return;
      ctx.fillStyle = patterns[g].fill;
      ctx.fillRect(left, a.top, right - left, a.bottom - a.top);
    });
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const o = chart.options.plugins.trafficMarks;
    if (!o || !o.data || o.data.picked === null || o.data.picked === undefined) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    const px = x.getPixelForValue(middle(o.data, o.data.picked));
    if (px < a.left || px > a.right) return;
    ctx.save();
    ctx.strokeStyle = o.ink;
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, a.top);
    ctx.lineTo(px, a.bottom);
    ctx.stroke();
    ctx.restore();
  }
};

// The x axis' ticks, as the Speed card's: every 4 hours up to a day, a day up to a week, 5 days up to a month, 15 days up to
// 90 days, the first of each month past that; counted back from the end of the range.
function ticksOf(min, max) {
  const days = (max - min) / 86400000;
  if (monthsPast((max - min) / 1000)) return monthTicks(min, max).map((value) => ({ value }));
  const step = days <= 1.05 ? 4 * 3600000 : (days <= 7.05 ? 1 : days <= 31 ? 5 : 15) * 86400000;
  const out = [];
  for (let v = max; v >= min; v -= step) out.unshift({ value: v });
  return out;
}

// Draws `c` into `canvas`. Returns {update(c, force), unmount()}; update draws again only for new data (or `force`, a new theme).
export function hoursChart(canvas, { pickable = false } = {}) {
  let chart = null;
  let shown = null;
  let cur = null;

  const xMin = () => (cur.hours.length ? cur.hours[0] * 1000 : 0);
  const xMax = () => (cur.ends.length ? cur.ends[cur.ends.length - 1] * 1000 : 1);

  function marksOptions() {
    const patterns = {};
    Object.entries(GAPS).forEach(([kind, [, tone]]) => { if (cur.gaps.includes(kind)) patterns[kind] = { fill: hatch(canvas, tone) }; });
    return { data: cur, patterns, ink: token('--ink-3', canvas) };
  }

  function options() {
    return {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      layout: { padding: { right: 8, top: 8, bottom: 2 } },
      onHover: pickable ? (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; } : undefined,
      scales: {
        x: {
          type: 'linear', min: xMin(), max: xMax(), grid: { display: false },
          afterBuildTicks(scale) { scale.ticks = ticksOf(xMin(), xMax()); },
          ticks: { font: { size: 10 }, maxRotation: 0, callback: (v) => axisLabel(v, (xMax() - xMin()) / 1000) }
        },
        y: {
          beginAtZero: true, grace: '8%', afterFit: (scale) => { scale.width = 58; },
          ticks: { font: { size: 10 }, maxTicksLimit: 5, callback: (v) => formatBytes(v) },
          title: { display: true, text: cur.unit === 'day' ? 'per day' : 'per hour', font: { size: 10 } }
        }
      },
      plugins: {
        trafficMarks: marksOptions(),
        legend: { display: false },
        tooltip: {
          filter: (item) => item.raw && item.raw.y !== null && item.raw.y !== undefined,
          callbacks: {
            title: (items) => (items.length ? cur.titles[items[0].dataIndex] : ''),
            label: (item) => `${item.dataset.label}: ${formatBytes(item.raw.y)}`
          }
        }
      }
    };
  }

  function nearest(e) {
    const els = chart && cur ? chart.getElementsAtEventForMode(e, 'nearest', { axis: 'x', intersect: false }, true) : [];
    const i = els.length ? els[0].index : -1;
    canvas.dataset.arg = i >= 0 ? String(cur.hours[i]) : '';
    canvas.dataset.arg2 = i >= 0 ? String(cur.ends[i]) : '';
  }
  if (pickable) canvas.addEventListener('click', nearest);

  return {
    update(c, force) {
      const sig = JSON.stringify([c.hours, c.ends, c.down, c.up, c.gaps, c.titles, c.unit, c.picked]);
      canvas.setAttribute('aria-label', c.label);
      if (chart && sig === shown && !force) return;
      shown = sig;
      cur = c;
      const sets = datasets(c, canvas);
      if (!chart) {
        chart = new Chart(canvas, { type: 'line', data: { datasets: sets }, options: options(), plugins: [marks] });
        return;
      }
      chart.data.datasets = sets;
      chart.options = options();
      chart.update('none');
    },
    unmount() { if (chart) chart.destroy(); chart = null; shown = null; if (pickable) canvas.removeEventListener('click', nearest); }
  };
}
