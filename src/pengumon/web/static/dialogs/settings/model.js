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

// What the speed test uses a month at these settings, as a sentence: "About 1.1 GB a month (daily, 25 MB down + 10 MB up)."
// (30 days of tests at the interval, each at its most: the window and a busy line only make it less). A direction stops
// after about 8 s of data (speedtest/runner.py TARGET_SECONDS), so with the plan speed known (Your line) a cap larger
// than 9 s of it is not reached: the estimate counts what the plan moves in 9 s instead (8 s, and the slow start) and
// says so: "About 9.4 GB a month (daily, about 310 of the 1,000 MB down + 4.5 of the 50 MB up: a test stops after
// about 8 s at your plan speed)." Over a monthly limit it says when the limit stops the tests: "...; the 2,000 MB limit
// stops tests after about day 3."
const WORDS = { 1: 'hourly', 24: 'daily', 168: 'weekly' };

// A number of hours (the speed test's interval) in the largest unit it is a whole number of: 36 -> 36 hours, 48 -> 2 days,
// 336 -> 2 weeks. `splitHours` gives the box and the unit the Settings row shows; `hoursText` the words.
export const HOUR_UNITS = [['weeks', 168, 'week'], ['days', 24, 'day'], ['hours', 1, 'hour']];
export function splitHours(hours) {
  const h = Number(hours);
  if (!(h > 0) || !Number.isInteger(h)) return { count: hours ?? '', unit: 'hours' };
  const [unit, size] = HOUR_UNITS.find(([, s]) => h % s === 0);
  return { count: h / size, unit };
}
export function hoursText(hours) {
  const { count, unit } = splitHours(hours);
  if (typeof count !== 'number') return String(hours ?? '');
  const [, , one] = HOUR_UNITS.find(([u]) => u === unit);
  return count === 1 ? `1 ${one}` : `${grouped(count)} ${unit}`;
}
// The hours a typed count and unit mean, or {problem} in words: a whole number, 1 hour to `max` hours.
export function readHours(countText, unit, max) {
  const text = String(countText ?? '').trim();
  const size = (HOUR_UNITS.find(([u]) => u === unit) || [])[1];
  if (text === '') return { problem: 'Enter a value.' };
  const n = Number(text);
  if (!Number.isFinite(n) || !size) return { problem: 'Enter a number.' };
  if (!Number.isInteger(n)) return { problem: 'Enter a whole number.' };
  const hours = n * size;
  if (hours < 1 || (max && hours > max)) return { problem: `Enter from 1 hour to ${hoursText(max)}.` };
  return { stored: hours };
}
const grouped = (n) => Math.round(n).toLocaleString('en-US');
const TEST_SECONDS = 9; // what a direction that stops by time moves, in seconds of the plan speed
const short = (n) => (n >= 10 ? grouped(n) : String(Math.round(n * 10) / 10));
export function speedEstimate(intervalHours, downMb, upMb, capMb, planDown, planUp) {
  const hours = Number(intervalHours);
  if (!(hours > 0) || !Number.isFinite(Number(downMb) + Number(upMb))) return '';
  // what one direction moves: its cap, or 9 s of the plan speed when that is less
  const moved = (cap, plan) => {
    const byTime = Number(plan) > 0 ? (Number(plan) * TEST_SECONDS) / 8 : Infinity;
    return byTime < Number(cap) ? { mb: byTime, text: `about ${short(byTime)} of the ${grouped(cap)} MB` } : { mb: Number(cap), text: `${cap} MB` };
  };
  const down = moved(downMb, planDown);
  const up = moved(upMb, planUp);
  const perDay = (24 / hours) * (down.mb + up.mb);
  const month = perDay * 30; // tests a month: 720 / hours (under one for an interval over 30 days)
  const size = month >= 1000 ? `${Math.round(month / 100) / 10} GB` : `${short(month)} MB`;
  const how = WORDS[hours] || `every ${hoursText(hours)}`;
  const byTime = down.mb < Number(downMb) || up.mb < Number(upMb);
  let text = `About ${size} a month (${how}, ${down.text} down + ${up.text} up${byTime ? ': a test stops after about 8 s at your plan speed' : ''})`;
  const cap = Number(capMb);
  if (cap > 0 && month > cap) text += `; the ${grouped(cap)} MB limit stops tests after about day ${Math.max(1, Math.ceil(cap / perDay))}`;
  return `${text}.`;
}

const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

// The choice of `spec` that `text` (an option's value, always a string) names, keeping its type: a number stays a number
// (the speed test's hours and megabytes). undefined when it is none of them.
export function choiceFor(spec, text) {
  return (spec.choices || []).find((c) => String(c) === String(text));
}

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
  if (spec.type === 'choice') return choiceFor(spec, value) !== undefined ? '' : 'Pick one of the choices.';
  if (spec.type === 'text' && spec.max_length && value.length > spec.max_length) return `At most ${spec.max_length} characters.`;
  return '';
}

// The stored value a typed `text` means: null for an empty nullable box, a number for a numeric field, else the text.
export function parse(spec, text, roundSeconds) {
  const value = String(text ?? '').trim();
  if (value === '') return null;
  return isNumeric(spec) ? fromShown(spec, Number(value), roundSeconds) : value;
}
