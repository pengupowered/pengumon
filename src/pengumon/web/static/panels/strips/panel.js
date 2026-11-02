// Uptime: one strip per target in path order (GET /api/uptime): 96 blocks of 15 minutes, or days, each ok, degraded,
// outage, busy (explained by line load) or no data, with the availability over the window. A name opens the target's
// history; every block lists the incidents of its time in the Incidents panel (showIncidentWindow, panels/incidents),
// which says so when there are none. Refreshed every five minutes. Rows are named as Path health names them (Router, Line, ISP hop N, Internet
// <address>, DNS lookup N), with the configured name beside it. A hop that ignores ping (no reply while a later hop answers:
// path.js quietTargets, from the live `targets` topic) gets no blocks, only the words Path health gives it: its strip
// would be one long outage that is none, and it neither counts as an outage nor opens incidents. So does an IPv6 target
// while this host has no IPv6 (its own words: "IPv6 unavailable on this host").

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { escapeHtml, getTargetInfo, percentText, targetList } from '../../js/util.js';
import { quietKey, quietTargets, shortLabels } from '../../js/path.js';

const CELL = { ok: '', degraded: 'w', outage: 'c', busy: 'b', no_data: 'n' };
const WORD = { ok: 'no loss', degraded: 'loss or a dead minute', outage: 'outage', busy: 'busy line (expected)', no_data: 'no data' };
const META = { '24h': '15-min blocks, path order', '7d': 'daily blocks, 7 days, path order', '30d': 'daily blocks, 30 days, path order' };
let win = '24h';
let seq = 0;
let shown = null; // the last answer, drawn again when the hops that ignore ping change
let quiet = new Map(); // target id -> its neutral words (path.js quietTargets)

const tz = () => -new Date().getTimezoneOffset(); // minutes east of UTC, as the API takes it
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const day = (ts) => new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });

function blockLabel(data, i) {
  const t = data.start + i * data.block_seconds;
  return data.block_seconds < 86400 ? `${clock(t)}–${clock(t + data.block_seconds)}` : day(t + 3600);
}

function axis(data) {
  const n = 4;
  return Array.from({ length: n + 1 }, (_, k) => {
    if (k === n) return 'now';
    const t = data.start + ((data.end - data.start) * k) / n;
    return data.block_seconds < 86400 ? clock(t) : new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  });
}

export function renderStrips(data) {
  shown = data;
  const body = document.getElementById('stripsBody');
  if (!data.targets.length) {
    body.innerHTML = '<div class="empty" style="grid-column: 1 / -1"><b>No probe data in this window yet.</b></div>';
    return;
  }
  const labels = shortLabels(targetList());
  const rows = data.targets.map((t) => {
    const name = getTargetInfo(t.target_id).name || t.name;
    const short = labels[t.target_id] || name;
    const sub = short === name ? '' : `<small>${escapeHtml(name)}</small>`;
    const head = `<button type="button" class="s-name" data-action="openTargetHistory" data-arg="${escapeHtml(t.target_id)}" title="${escapeHtml(name)}"><span>${escapeHtml(short)}</span>${sub}</button>`;
    if (quiet.has(t.target_id)) {
      const q = quiet.get(t.target_id);
      return `${head}<div class="strip-quiet" role="img" title="${escapeHtml(q.why)}" aria-label="${escapeHtml(`${short} (${name}): ${q.strip}`)}">${escapeHtml(q.strip)}</div>`
        + `<span class="s-pct num" title="${escapeHtml(`${q.word}: not counted`)}">–</span>`;
    }
    const cells = t.cells.map((c, i) => {
      const tip = `${escapeHtml(short)} · ${blockLabel(data, i)} · ${WORD[c] || c}`;
      const start = data.start + i * data.block_seconds;
      const label = `${short} · ${blockLabel(data, i)}`;
      return `<button type="button" class="cell ${CELL[c] ?? 'n'}" tabindex="${i ? -1 : 0}" data-action="showIncidentWindow" data-start="${start}" data-end="${start + data.block_seconds}"`
        + ` data-label="${escapeHtml(label)}" data-state="${escapeHtml(c)}" data-tip="${tip} · click for its incidents" aria-label="${escapeHtml(`${label}, ${WORD[c] || c}: show the incidents`)}"></button>`;
    }).join('');
    const bad = t.cells.filter((c) => c === 'outage').length;
    const warn = t.cells.filter((c) => c === 'degraded').length;
    const pct = percentText(t.availability_pct);
    return head
      + `<div class="strip" style="--n:${t.cells.length}" role="group" data-roving aria-label="${escapeHtml(short)} (${escapeHtml(name)}): ${bad} outage blocks, ${warn} degraded blocks, ${pct} available">${cells}</div>`
      + `<span class="s-pct num">${pct}</span>`;
  }).join('');
  body.innerHTML = `<span class="s-hd">Target</span><span class="s-hd"></span><span class="s-hd r">Avail.</span>${rows}
    <span></span><div class="s-axis">${axis(data).map((a) => `<span>${a}</span>`).join('')}</div><span></span>`;
  body.dataset.blocks = String(data.blocks);
}

async function load() {
  const mine = ++seq;
  document.getElementById('stripsMeta').textContent = META[win];
  document.querySelectorAll('#stripsRanges button').forEach((b) => {
    b.classList.toggle('active', b.dataset.arg === win);
    b.setAttribute('aria-pressed', String(b.dataset.arg === win));
  });
  try {
    const res = await fetch(`/api/uptime?window=${win}&tz_offset_minutes=${tz()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (mine === seq) renderStrips(data);
  } catch (e) {
    if (mine === seq) document.getElementById('stripsBody').innerHTML = `<div class="empty" style="grid-column: 1 / -1">Could not load the uptime: ${escapeHtml(e.message)}</div>`;
  }
}

registerActions('click', {
  setUptimeWindow: (el) => { win = el.dataset.arg; load(); }
});

export default {
  id: 'strips',
  title: 'Uptime',
  tab: 'live',
  icon: 'grid',
  core: true,
  available: () => true,
  mount() {
    load();
    setInterval(load, 300000);
    bus.subscribe('targets', (targets) => {
      const next = quietTargets(targets);
      if (quietKey(next) === quietKey(quiet)) return;
      quiet = next;
      if (shown) renderStrips(shown);
    });
  }
};
