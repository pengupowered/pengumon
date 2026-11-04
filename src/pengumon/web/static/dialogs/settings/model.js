// The Settings page's arithmetic: stored units to shown units and back, the browser-side range checks that read the
// schema (the server checks again, with the same ranges), and comparing values. No DOM here.

// A value as JSON with sorted keys, so two equal objects compare equal whatever their key order.
export function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`;
  if (v && typeof v === 'object') return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(',')}}`;
  return JSON.stringify(v === undefined ? null : v);
}
export const same = (a, b) => canon(a) === canon(b);
// Two lists of items: an item's field that is null counts as absent. The server fills a saved item's unset optional
// fields with null (a new host app check comes back with ca_file: null), and that is the item the page sent.
const bare = (v) => (Array.isArray(v) ? v.map(bare) : v && typeof v === 'object'
  ? Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null && x !== undefined).map(([k, x]) => [k, bare(x)])) : v);
export const sameItems = (a, b) => same(bare(a), bare(b));
export const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

const tidy = (n) => Number(n.toPrecision(12)); // 300.00000000000006 -> 300

// What a number is shown in: stored rounds are seconds (x round_seconds), the rest are x scale (a fraction as %, seconds as min).
export const factor = (spec, roundSeconds) => (spec.rounds ? roundSeconds : spec.scale || 1);
export const toShown = (spec, stored, roundSeconds) => (typeof stored === 'number' ? tidy(stored * factor(spec, roundSeconds)) : stored);
export const fromShown = (spec, shown, roundSeconds) => tidy(shown / factor(spec, roundSeconds));

export const isNumeric = (spec) => spec.type === 'number' || spec.type === 'integer';

// "15 %", "3 s", "5 min", "2 days", "on"/"off", "not set"; the words a row's default line and a badge use.
export function fmt(spec, stored, roundSeconds) {
  if (stored === null || stored === undefined || stored === '') return 'not set';
  if (spec.type === 'boolean') return stored ? 'on' : 'off';
  if (Array.isArray(stored)) return stored.length ? stored.join(', ') : 'default';
  if (isNumeric(spec)) {
    const n = toShown(spec, stored, roundSeconds);
    const text = String(n).includes('e') ? n.toFixed(12).replace(/0+$/, '') : String(n); // 0.000001, not 1e-6
    return spec.unit ? `${text} ${spec.unit}` : text;
  }
  return String(stored);
}

const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

// Why `text` (what is typed) is not an acceptable value for `spec`, in words, or '' when it is. A nullable field accepts
// an empty box (that means "not set").
export function problem(spec, text, roundSeconds) {
  const value = String(text ?? '').trim();
  if (value === '') return spec.nullable ? '' : 'Enter a value.';
  if (isNumeric(spec)) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 'Enter a number.';
    const stored = fromShown(spec, n, roundSeconds);
    if (spec.type === 'integer' && !Number.isInteger(stored)) return spec.rounds ? 'Enter a whole number of seconds.' : 'Enter a whole number.';
    const unit = spec.unit ? ` ${spec.unit}` : '';
    const shown = (x) => String(toShown(spec, x, roundSeconds));
    const low = spec.min !== null && spec.min !== undefined && (spec.exclusive_min ? stored <= spec.min : stored < spec.min);
    const high = spec.max !== null && spec.max !== undefined && (spec.exclusive_max ? stored >= spec.max : stored > spec.max);
    if (low || high) {
      const hasMin = spec.min !== null && spec.min !== undefined;
      const hasMax = spec.max !== null && spec.max !== undefined;
      if (hasMin && hasMax) return `Enter a value from ${shown(spec.min)} to ${shown(spec.max)}${unit}.`;
      return hasMin ? `Enter ${spec.exclusive_min ? 'more than' : 'at least'} ${shown(spec.min)}${unit}.` : `Enter at most ${shown(spec.max)}${unit}.`;
    }
    return '';
  }
  if (spec.type === 'time') return TIME.test(value) ? '' : 'Enter a time as HH:MM.';
  if (spec.type === 'choice') return (spec.choices || []).includes(value) ? '' : 'Pick one of the choices.';
  if (spec.type === 'text' && spec.max_length && value.length > spec.max_length) return `At most ${spec.max_length} characters.`;
  return '';
}

// The stored value a typed `text` means: null for an empty nullable box, a number for a numeric field, else the text.
export function parse(spec, text, roundSeconds) {
  const value = String(text ?? '').trim();
  if (value === '') return null;
  return isNumeric(spec) ? fromShown(spec, Number(value), roundSeconds) : value;
}
