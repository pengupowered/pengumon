// The figures under the title: the five stat cells, the status badge, the units, and the loss of a list of history rows.

import { badge } from '../../js/dom.js';
import { labels } from './state.js';

const LEVEL = { ok: 'good', degraded: 'warn', outage: 'crit', busy: 'busy', no_data: 'unknown' };
const STATE_WORD = { ok: 'Healthy', degraded: 'Degraded', outage: 'Down', busy: 'Busy', no_data: 'No data' };
export const num = (v) => typeof v === 'number' && Number.isFinite(v);
const fmt = (v, digits = 1) => (num(v) ? v.toFixed(digits) : '–');

export function setStats(cells, names = labels()) {
  document.querySelectorAll('#targetHistStats .stat').forEach((el, i) => {
    el.querySelector('.lbl').textContent = names[i];
    el.querySelector('.val').innerHTML = cells[i];
  });
}
export const unit = (v, u, digits = 1) => (num(v) ? `${fmt(v, digits)}<small>${u}</small>` : '–');
export const stateBadge = (cell) => (cell ? badge(LEVEL[cell] || 'unknown', STATE_WORD[cell] || 'No data') : '–');
export const per = (bucket) => (bucket >= 60 ? `${bucket / 60} min` : `${bucket} s`);

// The figures of a list of history rows: sent and answered probes give the loss (the bucket average without counts).
export function lossOf(rows, c) {
  const sent = c.tot >= 0 ? rows.reduce((a, r) => a + (r[c.tot] || 0), 0) : 0;
  const got = c.ok >= 0 ? rows.reduce((a, r) => a + (r[c.ok] || 0), 0) : 0;
  return sent > 0 ? ((sent - got) * 100) / sent : (rows.length ? rows.reduce((a, r) => a + (r[c.loss] || 0), 0) / rows.length : null);
}
