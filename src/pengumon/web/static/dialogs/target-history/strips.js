// The 24 hour strips: a row of blocks per target (from GET /api/uptime), the axis, the quiet-hop words, the address-change
// markers laid over a strip.

import { bus } from '../../js/bus.js';
import { zonedFormat } from '../../js/clock.js';
import { escapeHtml } from '../../js/escape.js';
import { quietTargets } from '../../js/path.js';
import { num } from './figures.js';

const CELL = { ok: '', degraded: 'w', outage: 'c', busy: 'b', no_data: 'n' };
const WORD = { ok: 'no loss', degraded: 'loss or a dead minute', outage: 'outage', busy: 'busy line (expected)', no_data: 'no data' };

// Block times on the clock the server cut the blocks on (their bounds and zone: /api/uptime), one formatter per strip.
const stripClock = (data) => zonedFormat(data, { hour: '2-digit', minute: '2-digit' });
function stripCells(data, t, name) {
  const hm = stripClock(data);
  const at = (i) => (data.bounds ? data.bounds[i] : data.start + i * data.block_seconds);
  return t.cells.map((c, i) => `<i class="${CELL[c] ?? 'n'}" data-tip="${escapeHtml(name)} · ${hm(at(i))}–${hm(at(i + 1))} · ${escapeHtml(WORD[c] || String(c))}"></i>`).join('');
}
const stripAxis = (data) => {
  const hm = stripClock(data);
  return `<div class="tgt-axis"><span>${hm(data.start)}</span><span>${hm((data.start + data.end) / 2)}</span><span>now</span></div>`;
};

function stripLabel(t, name) {
  const bad = t.cells.filter((c) => c === 'outage').length;
  const warn = t.cells.filter((c) => c === 'degraded').length;
  const avail = num(t.availability_pct) ? `${t.availability_pct.toFixed(2)}%` : 'unknown';
  return `${name}: ${bad} outage blocks, ${warn} degraded blocks, ${avail} available over 24 hours`;
}

// Where a hop that ignores ping, or an IPv6 target this host cannot probe, would have its strip (path.js quietTargets):
// the words Path health gives it (`q`), no blocks.
const quietStrip = (name, q) => `<div class="strip-quiet" role="img" title="${escapeHtml(q.why)}" aria-label="${escapeHtml(`${name}: ${q.strip}`)}">${escapeHtml(q.strip)}</div>`;
export const liveQuiet = () => quietTargets(bus.last('targets') || []);

// The 24 h strip of this target and the figures that come with it: its latest block's status and availability.
// The markers of the address changes inside a strip's 24 hours, over the strip.
function stripMarks(data, marks) {
  if (!data || !marks.length) return '';
  const span = data.end - data.start;
  return marks.filter((m) => m.x / 1000 >= data.start && m.x / 1000 <= data.end).map((m) => {
    const left = (((m.x / 1000) - data.start) / span) * 100;
    return `<i class="tgt-mark" style="left:${left.toFixed(2)}%" title="${escapeHtml(m.text)}" aria-hidden="true"></i>`;
  }).join('');
}

export function renderUptime(data, id, name, quiet, marks = []) {
  const box = document.getElementById('targetHistStrip');
  const t = data && data.targets.find((x) => x.target_id === id);
  if (quiet) {
    box.innerHTML = `${quietStrip(name, quiet)}${data ? stripAxis(data) : ''}`;
    return null;
  }
  if (!t) {
    box.innerHTML = '<div class="empty compact"><div>No uptime record for this target yet.</div></div>';
    return null;
  }
  const over = stripMarks(data, marks);
  box.innerHTML = `<div class="strip" style="--n:${t.cells.length}" role="img" aria-label="${escapeHtml(stripLabel(t, name))}">${stripCells(data, t, name)}</div>${over ? `<div class="tgt-marks">${over}</div>` : ''}${stripAxis(data)}`;
  return t;
}

// A group's 24 h strips: one row per member, named as the switcher names it.
export function renderGroupUptime(data, members, quiet) {
  const box = document.getElementById('targetHistStrip');
  const rows = members.map((m) => {
    const t = data && data.targets.find((x) => x.target_id === m.id);
    const name = `<span class="tgt-sn" title="${escapeHtml(m.name)}">${escapeHtml(m.short)}</span>`;
    if (quiet.has(m.id)) return `${name}${quietStrip(m.short, quiet.get(m.id))}<span class="num" title="${escapeHtml(`${quiet.get(m.id).word}: not counted`)}">–</span>`;
    if (!t) return `${name}<div class="tgt-none">no uptime record yet</div><span class="num">–</span>`;
    return `${name}<div class="strip" style="--n:${t.cells.length}" role="img" aria-label="${escapeHtml(stripLabel(t, m.short))}">${stripCells(data, t, m.short)}</div>`
      + `<span class="num">${num(t.availability_pct) ? `${t.availability_pct.toFixed(2)}%` : '–'}</span>`;
  }).join('');
  box.innerHTML = `<div class="tgt-strips">${rows}<span></span>${data ? stripAxis(data) : '<span></span>'}<span></span></div>`;
  return new Map(members.map((m) => [m.id, data && data.targets.find((x) => x.target_id === m.id)]));
}
