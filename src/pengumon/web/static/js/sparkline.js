// A tiny shared sparkline: one small <canvas> per use, drawn with the 2D API (no Chart.js instance), so redrawing a
// row of the hop matrix every second costs a few microseconds. Losses (null samples) are small red ticks at the
// bottom, gaps in the line. A redraw is skipped when the samples have not changed. A theme's chart style can ask for
// bars instead (theme.json "chart": {"spark": "bars"}, docs/themes.md "Chart style"): a bar per probe as tall as its
// round trip, a lost probe a full-height red bar; both sparklines here follow it.

const WIDTH = 80;
const HEIGHT = 22;
const SLOTS = 60; // x positions: the probe history window (the newest sample is at the right edge)
import { chartStyle, token } from './chart-theme.js';

const barsWanted = () => chartStyle()?.spark === 'bars';

let lossColor = ''; // the --crit token, read once per theme
document.addEventListener('themechange', () => { lossColor = ''; });
const TICK = 6;

export function createSparkline(label) {
  const canvas = document.createElement('canvas');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(WIDTH * dpr);
  canvas.height = Math.round(HEIGHT * dpr);
  canvas.className = 'sparkline';
  canvas.setAttribute('role', 'img');
  if (label) canvas.setAttribute('aria-label', label);
  return canvas;
}

// Draws `values` (milliseconds, null for a loss) in `color`. Returns true when it drew.
export function drawSparkline(canvas, values, color) {
  if (!lossColor) lossColor = token('--crit') || 'red';
  const bars = barsWanted();
  const signature = `${bars ? 'bars|' : ''}${color}|${lossColor}|${values.join(',')}`;
  if (canvas.dataset.sig === signature) return false;
  canvas.dataset.sig = signature;
  const ctx = canvas.getContext('2d');
  const scale = canvas.width / WIDTH;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.clearRect(0, 0, WIDTH, HEIGHT);
  const n = Math.max(SLOTS, values.length);
  const offset = n - values.length;
  const x = (i) => ((i + offset) / (n - 1)) * (WIDTH - 2) + 1;
  let max = 1;
  for (const v of values) if (v !== null && v !== undefined && v > max) max = v;
  if (bars) {
    const slot = (WIDTH - 2) / n;
    values.forEach((v, i) => {
      const lost = v === null || v === undefined;
      const h = lost ? HEIGHT : Math.max(2, (v / max) * (HEIGHT - 2));
      ctx.fillStyle = lost ? lossColor : color;
      ctx.fillRect(1 + (i + offset) * slot, HEIGHT - h, Math.max(1, slot * 0.7), h);
    });
    return true;
  }
  const top = TICK; // room for the loss ticks below the line
  const y = (v) => HEIGHT - 2 - (v / max) * (HEIGHT - top - 3);

  ctx.lineWidth = 1.25;
  ctx.lineJoin = 'round';
  ctx.strokeStyle = color;
  ctx.beginPath();
  let penDown = false;
  values.forEach((v, i) => {
    if (v === null || v === undefined) { penDown = false; return; }
    if (penDown) ctx.lineTo(x(i), y(v));
    else {
      ctx.moveTo(x(i), y(v));
      ctx.lineTo(x(i) + 0.01, y(v)); // a lone sample still shows as a dot
      penDown = true;
    }
  });
  ctx.stroke();

  ctx.fillStyle = lossColor;
  values.forEach((v, i) => {
    if (v === null || v === undefined) ctx.fillRect(Math.round(x(i)) - 1, HEIGHT - TICK, 2, TICK);
  });
  return true;
}

// The design's SVG sparkline (path health nodes, service rows): status-colored line over a gradient wash, red ticks
// for lost probes, a dot on the newest value; no data is a flat dashed line. Returns markup; colors are tokens.
// `id` names the series (a path node's key): its gradient id is `sg-<id>`, so the same samples give the same markup and a
// caller can tell by comparing strings whether anything changed. Without one the id follows the level.
const SPARK_COLOR = { good: 'var(--accent)', warn: 'var(--warn)', crit: 'var(--crit)', busy: 'var(--busy)', unknown: 'var(--unknown)' };

export function sparkSvg(vals, level = 'good', { h = 30, w = 120, id: series = level } = {}) {
  const c = SPARK_COLOR[level] || SPARK_COLOR.good;
  const has = (v) => v !== null && v !== undefined;
  if (!vals || !vals.some(has)) {
    const lost = vals && vals.length ? vals.map((_, i) => `<rect x="${(i * (w / vals.length)).toFixed(1)}" y="${h - 6}" width="1.4" height="6" fill="var(--crit)"/>`).join('') : '';
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><line x1="0" x2="${w}" y1="${h / 2}" y2="${h / 2}" stroke="var(--line-strong)" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>${lost}</svg>`;
  }
  if (barsWanted()) {
    const top = Math.max(...vals.filter(has)) || 1;
    const n = Math.max(30, vals.length); // at least 30 slots, the newest on the right, so a short history stays thin bars
    const slot = w / n;
    const bars = vals.map((v, i) => {
      const bh = has(v) ? Math.max(2, (v / top) * (h - 2)) : h;
      return `<rect x="${((i + n - vals.length) * slot).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${(slot * 0.7).toFixed(2)}" height="${bh.toFixed(1)}" fill="${has(v) ? c : 'var(--crit)'}"/>`;
    }).join('');
    return `<svg class="spark bars" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${bars}</svg>`;
  }
  const n = Math.max(2, vals.length);
  const mx = Math.max(...vals.filter(has)) * 1.15 || 1;
  const X = (i) => (i / (n - 1)) * (w - 2) + 1;
  const Y = (v) => h - 3 - (v / mx) * (h - 7);
  let d = '';
  let pen = false;
  vals.forEach((v, i) => {
    if (!has(v)) { pen = false; return; }
    d += `${pen ? 'L' : 'M'}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`;
    pen = true;
  });
  const id = `sg-${String(series).replace(/[^\w-]/g, '_')}`;
  const first = vals.findIndex(has);
  let last = vals.length - 1;
  while (!has(vals[last])) last--;
  const ticks = vals.map((v, i) => (has(v) ? '' : `<rect x="${(X(i) - 0.8).toFixed(1)}" y="${h - 6}" width="1.6" height="6" fill="var(--crit)"/>`)).join('');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${c}" stop-opacity=".28"/><stop offset="1" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>`
    + `<path d="${d}L${X(last).toFixed(1)} ${h}L${X(first).toFixed(1)} ${h}Z" fill="url(#${id})"/><path d="${d}" fill="none" stroke="${c}" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>${ticks}`
    + `<circle cx="${X(last).toFixed(1)}" cy="${Y(vals[last]).toFixed(1)}" r="2.2" fill="${c}"/></svg>`;
}
