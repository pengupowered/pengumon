// When it breaks: packet loss by day and hour over the last 7 days (GET /api/report/loss-grid), all targets together,
// on the sequential loss scale, with one sentence naming the hours where most loss falls. Every hour that has begun is a
// button that lists the incidents of that hour in the Incidents panel (showIncidentWindow, panels/incidents), which says
// so when there are none; an hour still to come is only a cell. The hours are the server's, on the clock of the zone it
// used (Settings, else this browser's): when each one begins comes with the answer (day_starts, and hour_starts on a day
// the clocks change), so a click lists the right stretch whatever this browser's zone. Refreshed every 15 minutes.

import { escapeHtml, zoneParams } from '../../js/util.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const hh = (h) => `${String(h % 24).padStart(2, '0')}:00`;
// When hour h (0..24) of day d begins, from the answer: an hour of a day the clocks change is listed, any other is
// that day's start plus h hours.
const hourStart = (grid, d, h) => grid.hour_starts[d]?.[h] ?? grid.day_starts[d] + h * 3600;

// loss % -> the sequential scale step (tokens --q0..--q4); null: no probes
function step(pct) {
  if (pct === null || pct === undefined) return null;
  return pct <= 0 ? 0 : pct < 0.5 ? 1 : pct < 2 ? 2 : pct < 5 ? 3 : 4;
}

// The 4-hour window with the most lost probes, and on how many days it had loss.
function insight(grid) {
  const lostByHour = Array.from({ length: 24 }, (_, h) => grid.lost.reduce((a, row) => a + (row[h] || 0), 0));
  const total = lostByHour.reduce((a, b) => a + b, 0);
  if (!total) return 'No packet loss in the last 7 days.';
  let best = 0;
  let bestSum = -1;
  for (let h = 0; h < 24; h++) {
    const sum = [0, 1, 2, 3].reduce((a, k) => a + lostByHour[(h + k) % 24], 0);
    if (sum > bestSum) { bestSum = sum; best = h; }
  }
  const days = grid.lost.filter((row) => [0, 1, 2, 3].some((k) => (row[(best + k) % 24] || 0) > 0)).length;
  const share = Math.round((bestSum / total) * 100);
  return `<b>${share}%</b> of the lost probes fall between <b>${hh(best)} and ${hh(best + 4)}</b>, on ${days} of the last ${grid.days.length} days. Worth naming when you call the provider.`;
}

export function renderHeat(grid) {
  const now = Date.now() / 1000;
  const head = `<span></span>${Array.from({ length: 24 }, (_, h) => `<span class="hd">${h % 6 === 0 ? String(h).padStart(2, '0') : ''}</span>`).join('')}`;
  const rows = grid.loss_pct.map((row, d) => {
    const name = DAYS[grid.weekdays[d]] || grid.days[d];
    return `<span>${name}</span>${row.map((pct, h) => {
      const q = step(pct);
      const fill = q === null ? 'var(--nodata)' : `var(--q${q})`;
      const what = q === null ? 'no probes' : pct > 0 ? `loss ${pct.toFixed(2)}% (${grid.lost[d][h]} of ${grid.sent[d][h]})` : 'no loss';
      const label = `${name} ${hh(h)}–${hh(h + 1)}`;
      const start = hourStart(grid, d, h);
      const end = hourStart(grid, d, h + 1);
      if (start > now) return `<i style="background:${fill}" data-tip="${escapeHtml(label)} · not yet"></i>`;
      // the hour the clocks go back happens twice, the one they skip not at all
      const clocks = end - start > 3600 ? ' (twice: the clocks went back)' : end <= start ? ' (none: the clocks went forward)' : '';
      return `<button type="button" class="cell" tabindex="${d + h ? -1 : 0}" style="background:${fill}" data-action="showIncidentWindow" data-start="${start}" data-end="${end}"`
        + ` data-label="${escapeHtml(label)}" data-lost="${grid.lost[d][h] || 0}" data-sent="${grid.sent[d][h] || 0}"`
        + ` data-tip="${escapeHtml(label + clocks)} · ${escapeHtml(what)} · click for its incidents" aria-label="${escapeHtml(`${label}${clocks}, ${what}: show the incidents`)}"></button>`;
    }).join('')}`;
  }).join('');
  const el = document.getElementById('heatGrid');
  el.innerHTML = head + rows;
  const text = insight(grid);
  const p = document.getElementById('heatInsight');
  p.hidden = false;
  const span = p.querySelector('span');
  span.innerHTML = text;
  // the plain text the browser made of it (not a tag-stripping regex)
  el.setAttribute('aria-label', `Packet loss by day and hour, last ${grid.days.length} days. ${span.textContent}`);
  document.getElementById('heatHint').hidden = !el.querySelector('.cell');
}

async function load() {
  try {
    const res = await fetch(`/api/report/loss-grid?days=7&${zoneParams()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderHeat(await res.json());
  } catch (e) {
    document.getElementById('heatGrid').innerHTML = `<div class="empty" style="grid-column: 1 / -1">Could not load the loss grid: ${escapeHtml(e.message)}</div>`;
  }
}

export default {
  id: 'heat',
  title: 'When it breaks',
  tab: 'incidents',
  icon: 'heat',
  core: true,
  available: () => true,
  mount() {
    load();
    setInterval(load, 900000);
  }
};
