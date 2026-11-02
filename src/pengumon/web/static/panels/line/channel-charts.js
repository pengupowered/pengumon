// The Channels dialog's per-channel bar charts (power and SNR, one bar per channel): inline SVG, drawn from the server's
// readings. The shaded good zone and the bar colours are the server's: the zone comes from the device's `ranges`, each
// bar's colour from the channel's own `grades` (no limit is written here).

import { escapeHtml } from '../../js/util.js';

const has = (v) => v !== null && v !== undefined;
const FILL = { good: 'var(--s1)', warn: 'var(--warn-fill)', critical: 'var(--crit)' };
const H = 120;
const LEFT = 38;

// Axis ticks at round steps inside [min, max] (about four of them).
export function ticksOf(min, max) {
  const raw = (max - min) / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

// One chart as markup. channels: the channels to draw in order; value(c) the reading; grade(c) the server's grade for it;
// range the device's range for this metric (null: drawn without a zone, from the data's own span).
export function barChartMarkup({ channels, value, grade, range, unit, label, width, zoneNote }) {
  const vals = channels.map(value).filter(has);
  if (!vals.length) return '<p class="muted">No readings.</p>';
  let min = range ? range.min : Math.min(...vals);
  let max = range ? range.max : Math.max(...vals);
  if (!(max > min)) { min -= 1; max += 1; }
  const W = Math.max(300, Math.round(width || 900));
  const y = (v) => 10 + (1 - (Math.min(max, Math.max(min, v)) - min) / (max - min)) * (H - 20);
  const base = min < 0 && max > 0 ? 0 : min; // power bars grow from zero, SNR bars from the bottom
  const slot = (W - LEFT) / channels.length;
  const bw = Math.max(3, Math.min(16, slot - 4));
  const goodLo = range && has(range.good_low) ? range.good_low : null;
  const goodHi = range && has(range.good_high) ? range.good_high : null;
  const zone = range && (has(goodLo) || has(goodHi))
    ? `<rect x="${LEFT}" y="${y(has(goodHi) ? goodHi : max)}" width="${W - LEFT}" height="${Math.max(1, y(has(goodLo) ? goodLo : min) - y(has(goodHi) ? goodHi : max))}" fill="var(--good)" opacity=".1"/>` : '';
  const grid = ticksOf(min, max).map((v) => `<line class="grid" x1="${LEFT}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${LEFT - 6}" y="${y(v) + 3.5}" text-anchor="end">${v}</text>`).join('');
  const bars = channels.map((c, i) => {
    const v = value(c);
    if (!has(v)) return '';
    const g = grade(c);
    const top = Math.min(y(v), y(base));
    const tip = `${c.direction === 'upstream' ? 'Up' : 'Down'} ${c.channel_id} · ${has(c.frequency_mhz) ? `${c.frequency_mhz} MHz · ` : ''}${v} ${unit}${g && g !== 'good' ? ` · ${g === 'critical' ? 'critical' : 'outside good range'}` : ''}`;
    return `<rect x="${(LEFT + i * slot + (slot - bw) / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(2, Math.abs(y(v) - y(base))).toFixed(1)}" rx="2" fill="${FILL[g] || 'var(--ink-3)'}" data-tip="${escapeHtml(tip)}"/>`;
  }).join('');
  const first = channels[0];
  const last = channels[channels.length - 1];
  const ends = (c) => `${c.direction === 'upstream' ? 'Up' : 'Down'} ${c.channel_id}${has(c.frequency_mhz) ? ` · ${c.frequency_mhz} MHz` : ''}`;
  const labelY = H + 14;
  const end = channels.length > 1 ? `<text x="${W}" y="${labelY}" text-anchor="end">${escapeHtml(ends(last))}</text>` : '';
  const goodLine = range && has(goodLo) && !has(goodHi) && goodLo > min ? `<line x1="${LEFT}" x2="${W}" y1="${y(goodLo)}" y2="${y(goodLo)}" stroke="var(--ink-2)" stroke-dasharray="4 4"/>` : '';
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H + 18}" role="img" aria-label="${escapeHtml(label)}${zoneNote ? `, ${escapeHtml(zoneNote)}` : ''}">${zone}${grid}${bars}${goodLine}<text x="${LEFT}" y="${labelY}">${escapeHtml(ends(first))}</text>${end}</svg></div>`;
}
