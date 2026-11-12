// The traffic chart (Chart.js bars): download and upload per clock hour (or per day: the card over 30 days), used by the card
// (view.js) and by the device dialog (client.js). It draws plain data: {labels, titles, down, up, gaps, label}, one entry per
// hour or day, where `down` and
// `up` are bytes or null for an hour with no figure, and `gaps[i]` says why: 'unpublished' (the router has not published the
// hour yet) or 'nodata' (never stored). A gap is a hatched bar full height, never a bar of 0: a router that has not told is
// not a router that moved nothing. Colors are tokens, read when it draws (a theme change draws it again).

import { series, token } from '../../js/chart-theme.js';
import { formatBytes } from '../../js/format.js';

const HEX6 = /^#[0-9a-f]{6}$/i;
// The two kinds of gap: its words and the token its hatching is drawn in. The newest hour not being published yet is the
// one a person looks for; a stretch that was never stored is quieter.
const GAPS = { unpublished: ['Not published yet', '--ink-3'], nodata: ['Not stored', '--line-strong'] };

// The upload bars are the quieter of the two: the download's neighbor color a little transparent (a color that is not a plain
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

// The chart's legend entries for the data: [{name, color, pattern}] (the hatched ones are drawn as hatching by the view).
export function legendOf(c, canvas) {
  const out = [{ name: 'Download', color: series(0, canvas) }, { name: 'Upload', color: quieter(series(1, canvas)) }];
  Object.entries(GAPS).forEach(([kind, [name, tone]]) => { if (c.gaps.includes(kind)) out.push({ name, color: token(tone, canvas), pattern: true }); });
  return out;
}

function datasets(c, canvas) {
  const peak = Math.max(1, ...c.down.filter((v) => v !== null), ...c.up.filter((v) => v !== null));
  const sets = [
    { label: 'Download', data: c.down, backgroundColor: series(0, canvas), borderWidth: 0, barPercentage: 0.9, categoryPercentage: 0.9, order: 1 },
    { label: 'Upload', data: c.up, backgroundColor: quieter(series(1, canvas)), borderWidth: 0, barPercentage: 0.9, categoryPercentage: 0.9, order: 1 }
  ];
  Object.entries(GAPS).forEach(([kind, [name, tone]]) => {
    if (!c.gaps.includes(kind)) return;
    // full height behind the bars (grouped: false puts them on the hour, not beside the other two)
    sets.push({
      label: name, gap: kind, data: c.gaps.map((g) => (g === kind ? peak : null)), grouped: false, order: 5,
      backgroundColor: hatch(canvas, tone), borderColor: token(tone, canvas), borderWidth: 1, borderSkipped: false, barPercentage: 1, categoryPercentage: 0.96
    });
  });
  return sets;
}

// Draws `c` into `canvas`. Returns {update(c), unmount()}; update draws again only for new data (or `force`, a new theme).
export function hoursChart(canvas) {
  let chart = null;
  let shown = null;
  let cur = null;

  function options() {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', axis: 'x', intersect: false },
      layout: { padding: { right: 6, top: 6, bottom: 2 } },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
        y: { beginAtZero: true, grace: '6%', afterFit: (scale) => { scale.width = 58; }, ticks: { font: { size: 10 }, maxTicksLimit: 5, callback: (v) => formatBytes(v) } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => item.raw !== null && item.raw !== undefined,
          callbacks: {
            title: (items) => (items.length ? cur.titles[items[0].dataIndex] : ''),
            label: (item) => (item.dataset.gap ? item.dataset.label : `${item.dataset.label}: ${formatBytes(item.raw)}`)
          }
        }
      }
    };
  }

  return {
    update(c, force) {
      const sig = JSON.stringify([c.labels, c.down, c.up, c.gaps, c.titles]);
      if (chart && sig === shown && !force) {
        canvas.setAttribute('aria-label', c.label);
        return;
      }
      shown = sig;
      cur = c;
      canvas.setAttribute('aria-label', c.label);
      const sets = datasets(c, canvas);
      if (!chart) {
        chart = new Chart(canvas, { type: 'bar', data: { labels: c.labels, datasets: sets }, options: options() });
        return;
      }
      chart.data.labels = c.labels;
      chart.data.datasets = sets;
      chart.update('none');
    },
    unmount() { if (chart) chart.destroy(); chart = null; shown = null; }
  };
}
