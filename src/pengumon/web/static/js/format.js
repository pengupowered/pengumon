// Number and word formatting: round-trip times, percentages, byte counts, id-safe words.

// A round-trip time in ms: a tenth below 100, whole above (`tenths`: always a tenth); `unit` adds " ms". No number: `empty`.
export function formatMs(v, { unit = false, empty = '–', tenths = false } = {}) {
  if (v === null || v === undefined || !Number.isFinite(+v)) return empty;
  const text = v >= 100 && !tenths ? String(Math.round(v)) : (+v).toFixed(1);
  return unit ? `${text} ms` : text;
}

// An id-safe word from free text: lower case, runs of other characters as one underscore, none at the ends.
export const slug = (text) => String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

// A percentage to `digits` decimals that never rounds to a clean 0 or 100 unless it is exactly that: 99.996 % available is
// not "100.00%", and one lost probe in a million is not "0.000%". Null or undefined: a dash.
export function percentText(v, digits = 2) {
  if (v === null || v === undefined) return '–';
  const text = v.toFixed(digits);
  if (v > 0 && Number(text) === 0) return `<${(10 ** -digits).toFixed(digits)}%`;
  if (v < 100 && Number(text) === 100) return `>${(100 - 10 ** -digits).toFixed(digits)}%`;
  return `${text}%`;
}

// A byte count in the units a router's own pages use (decimal): "75 GB", "2.2 GB", "350 MB", "12 kB", "0 B". Three figures
// are plenty: a tenth below 100, whole above (as the speed figures). No number: a dash.
export function formatBytes(n) {
  if (n === null || n === undefined || !Number.isFinite(+n)) return '–';
  const units = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
  let v = Math.max(0, +n);
  let i = 0;
  while (v >= 999.95 && i < units.length - 1) { v /= 1000; i++; }
  const text = i === 0 || v >= 100 ? String(Math.round(v)) : String(Math.round(v * 10) / 10);
  return `${text} ${units[i]}`;
}

// "1 client", "2 clients".
export const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
