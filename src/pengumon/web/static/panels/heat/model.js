// When it breaks, the model: packet loss by day and hour over the last 7 days (GET /api/report/loss-grid), all targets
// together, on the sequential loss scale, with one sentence naming the hours where most loss falls. Every hour that has
// begun is a button that lists the incidents of that hour in the Incidents panel (showIncidentWindow, panels/incidents),
// which says so when there are none; an hour still to come is only a cell. The hours are the server's, on the clock of
// the zone it used (Settings, else this browser's): when each one begins comes with the answer (day_starts, and
// hour_starts on a day the clocks change), so a click lists the right stretch whatever this browser's zone. Refreshed
// every 15 minutes. present() makes the view model every view draws (view.js is the standard one; docs/themes.md,
// "Drawing a card").

import { createCard } from '../../js/cards.js';
import { zoneParams } from '../../js/util.js';
import standard from './view.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const hh = (h) => `${String(h % 24).padStart(2, '0')}:00`;
// When hour h (0..24) of day d begins, from the answer: an hour of a day the clocks change is listed, any other is
// that day's start plus h hours.
const hourStart = (grid, d, h) => grid.hour_starts[d]?.[h] ?? grid.day_starts[d] + h * 3600;

let card = null;

// loss % -> the sequential scale step (0..4, tokens --q0..--q4); null: no probes
export function step(pct) {
  if (pct === null || pct === undefined) return null;
  return pct <= 0 ? 0 : pct < 0.5 ? 1 : pct < 2 ? 2 : pct < 5 ? 3 : 4;
}

// The 4-hour window with the most lost probes, and on how many days it had loss: the sentence as text and as parts
// (a part with `strong` is set in bold).
function insight(grid) {
  const lostByHour = Array.from({ length: 24 }, (_, h) => grid.lost.reduce((a, row) => a + (row[h] || 0), 0));
  const total = lostByHour.reduce((a, b) => a + b, 0);
  if (!total) return { text: 'No packet loss in the last 7 days.', parts: [{ text: 'No packet loss in the last 7 days.' }] };
  let best = 0;
  let bestSum = -1;
  for (let h = 0; h < 24; h++) {
    const sum = [0, 1, 2, 3].reduce((a, k) => a + lostByHour[(h + k) % 24], 0);
    if (sum > bestSum) { bestSum = sum; best = h; }
  }
  const days = grid.lost.filter((row) => [0, 1, 2, 3].some((k) => (row[(best + k) % 24] || 0) > 0)).length;
  const share = Math.round((bestSum / total) * 100);
  const parts = [{ text: '' }, { text: `${share}%`, strong: true }, { text: ' of the lost probes fall between ' }, { text: `${hh(best)} and ${hh(best + 4)}`, strong: true },
    { text: `, on ${days} of the last ${grid.days.length} days. Worth naming when you call the provider.` }];
  return { text: parts.map((p) => p.text).join(''), parts: parts.slice(1) };
}

// One hour as the views draw it: { id, future, q (0..4, null: no probes), label, tip, aria, start, end, lost, sent }.
function cell(grid, d, h, name, now) {
  const pct = grid.loss_pct[d][h];
  const q = step(pct);
  const what = q === null ? 'no probes' : pct > 0 ? `loss ${pct.toFixed(2)}% (${grid.lost[d][h]} of ${grid.sent[d][h]})` : 'no loss';
  const label = `${name} ${hh(h)}–${hh(h + 1)}`;
  const start = hourStart(grid, d, h);
  const end = hourStart(grid, d, h + 1);
  const id = `${d}-${h}`;
  if (start > now) return { id, future: true, q, label, tip: `${label} · not yet`, aria: `${label}, not yet` };
  // the hour the clocks go back happens twice, the one they skip not at all
  const clocks = end - start > 3600 ? ' (twice: the clocks went back)' : end <= start ? ' (none: the clocks went forward)' : '';
  return { id, future: false, q, label, start, end, lost: grid.lost[d][h] || 0, sent: grid.sent[d][h] || 0,
    tip: `${label + clocks} · ${what} · click for its incidents`, aria: `${label}${clocks}, ${what}: show the incidents` };
}

// The view model for an answer of the server: { state: 'ready', label, legend: {low, high}, hours: ['', '', ..], rows:
// [{id, name, cells}], hint, insight: {text, parts} }.
export function present(grid, now) {
  const insightNow = insight(grid);
  return {
    state: 'ready',
    label: `Packet loss by day and hour, last ${grid.days.length} days. ${insightNow.text}`,
    legend: { low: '0%', high: '≥ 5%' },
    hours: Array.from({ length: 24 }, (_, h) => (h % 6 === 0 ? String(h).padStart(2, '0') : '')),
    rows: grid.loss_pct.map((row, d) => {
      const name = DAYS[grid.weekdays[d]] || grid.days[d];
      return { id: String(d), name, cells: row.map((_, h) => cell(grid, d, h, name, now)) };
    }),
    hint: { text: 'Click an hour for its incidents', shown: grid.loss_pct.some((row, d) => row.some((_, h) => hourStart(grid, d, h) <= now)) },
    insight: insightNow
  };
}

// Made-up grid for the parity test of the card's views: two days, one hour with loss, one with none, the rest of the second
// day still to come.
export function fixture() {
  const start = 1767225600; // a fixed moment, a day's start
  const rows = (v) => Array.from({ length: 2 }, () => Array(24).fill(v));
  const lost = rows(0);
  const sent = rows(60);
  const pct = rows(0);
  lost[0][3] = 4; pct[0][3] = (4 / 60) * 100;
  pct[1][1] = null; sent[1][1] = 0;
  const grid = { days: ['2026-01-01', '2026-01-02'], weekdays: [3, 4], day_starts: [start, start + 86400, start + 172800], hour_starts: [null, null], sent, lost, loss_pct: pct };
  return present(grid, start + 86400 + 6 * 3600 + 30);
}

// Draws an answer.
function renderHeat(grid) {
  if (card) card.update(present(grid, Date.now() / 1000));
}

async function load() {
  try {
    const res = await fetch(`/api/report/loss-grid?days=7&${zoneParams()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderHeat(await res.json());
  } catch (e) {
    if (card) card.update({ state: 'error', error: `Could not load the loss grid: ${e.message}` });
  }
}

export function initHeat(section) {
  card = createCard({ panel: 'heat', root: section.querySelector('.panel-b'), standard, fixture });
  load();
  setInterval(load, 900000);
}
