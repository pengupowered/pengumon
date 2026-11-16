// Bars of brief latency spikes per day (or week): shared by the Incidents card's block and the reliability report's row, so
// both cut their cells the same way: a cell's width follows the hours it holds in the range (the first and last are cut).

const holdText = (h) => { const m = Math.round(h * 60); return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`; };
export const spikesText = (n) => `${n} spike${n === 1 ? '' : 's'}`;

// The columns of a strip of days: a day's width follows the hours it holds in the range (a cut first or last day is narrower).
export function stripStyle(days) {
  const first = days.length ? days[0].hours : 24;
  const grid = days.some((d) => d.hours !== first)
    ? days.map((d) => `minmax(0, ${Math.round(d.hours * 1000) / 1000}fr)`).join(' ')
    : `repeat(${Math.max(1, days.length)}, minmax(0, 1fr))`;
  return `grid-template-columns: ${grid}; gap: ${days.length > 60 ? 1 : days.length > 31 ? 2 : days.length > 14 ? 3 : 4}px`;
}

// Bars of counts: [{ id, tip, count, height, partial }]. `cells`: [{ id, name, count, hours, cut }].
export function stripBars(cells, unit = 'day') {
  const peak = Math.max(0, ...cells.map((c) => c.count));
  return cells.map((c) => ({ id: c.id, count: c.count, partial: c.cut, height: c.count > 0 && peak > 0 ? Math.max(12, (c.count / peak) * 100) : 0,
    tip: `${c.name} · ${spikesText(c.count)}${c.cut ? ` · only ${holdText(c.hours)} of this ${unit} are in the range` : ''}` }));
}

