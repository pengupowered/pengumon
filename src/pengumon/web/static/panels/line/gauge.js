// The range gauge as data: the bar's zones from the device's ranges, the words under and over it. Shared by the line
// card's view model (docsis.js `reader`), its standard view (view.js) and the Channels dialog's gauges (docsis.js), so
// the three draw one gauge the same way. Technology-free: a range is {unit, min, max, good_low, good_high, critical_low,
// critical_high} from the server.

const has = (v) => v !== null && v !== undefined;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

// The bar as segments, in drawing order: {kind: 'mid' | 'ok' | 'spread' | 'mark', left, width, title}. `lo`/`hi` are the
// spread of the value across the channels; no range: only a mark in the middle.
export function segments(range, value, lo, hi) {
  if (!range) return has(value) ? [{ kind: 'mark', left: '50%' }] : [];
  const pct = (v) => `${(((clamp(v, range.min, range.max) - range.min) / (range.max - range.min)) * 100).toFixed(2)}%`;
  const width = (a, b) => `calc(${pct(b)} - ${pct(a)})`;
  const out = [];
  const goodLo = has(range.good_low) ? range.good_low : range.min;
  const goodHi = has(range.good_high) ? range.good_high : range.max;
  const warnLo = has(range.critical_low) ? range.critical_low : range.min;
  const warnHi = has(range.critical_high) ? range.critical_high : range.max;
  if (goodLo > warnLo) out.push({ kind: 'mid', left: pct(warnLo), width: width(warnLo, goodLo) });
  if (warnHi > goodHi) out.push({ kind: 'mid', left: pct(goodHi), width: width(goodHi, warnHi) });
  out.push({ kind: 'ok', left: pct(goodLo), width: width(goodLo, goodHi) });
  if (has(lo) && has(hi) && hi > lo) out.push({ kind: 'spread', left: pct(lo), width: width(lo, hi), title: 'Spread across channels' });
  if (has(value)) out.push({ kind: 'mark', left: pct(value) });
  return out;
}

// The segments as markup (every value in it is computed here, none is the device's text).
export function barMarkup(segs) {
  return segs.map((s) => {
    if (s.kind === 'mark') return `<div class="g-mark" style="left:${s.left}"></div>`;
    if (s.kind === 'spread') return `<div class="g-spread" style="left:${s.left};width:${s.width}" title="${s.title}"></div>`;
    return `<div class="g-zone ${s.kind}" style="left:${s.left};width:${s.width}"></div>`;
  }).join('');
}

// "good 1 to 8", "good 33+", "good up to 8": where the good zone is.
export function goodText(range) {
  if (!range) return '';
  const lo = has(range.good_low) && range.good_low > range.min ? range.good_low : null;
  const hi = has(range.good_high) && range.good_high < range.max ? range.good_high : null;
  if (has(lo) && has(hi)) return `good ${lo} to ${hi}`;
  if (has(lo)) return `good ${lo}+`;
  return has(hi) ? `good up to ${hi}` : 'good';
}

// The words of one gauge: the bar's accessible name and the scale under it (min, the good zone with the spread, max).
export function gaugeText(name, range, value, spread) {
  const ch = spread ? `${range ? ' · ' : ''}channels ${spread[0].toFixed(1)} to ${spread[1].toFixed(1)}` : '';
  return {
    aria: `${name} ${has(value) ? `${(+value).toFixed(1)}${range ? ` ${range.unit}` : ''}` : 'no reading'}${range ? `, ${goodText(range)}` : ''}`,
    scale: { min: range ? String(range.min) : '', good: goodText(range) + ch, max: range ? String(range.max) : '' }
  };
}
