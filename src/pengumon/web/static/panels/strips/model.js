// Uptime, the model: one strip per target in path order (GET /api/uptime): 96 blocks of 15 minutes, or days, each ok,
// degraded, outage, busy (explained by line load) or no data, with the availability over the window. A name opens the
// target's history; every block lists the incidents of its time in the Incidents panel (showIncidentWindow,
// panels/incidents), which says so when there are none; its times are the server's (a day can have 23 or 25 hours), on
// the clock of the zone it used (Settings, else this browser's). Refreshed every five minutes. Rows are named as Path
// health names them (Router, Gateway, ISP hop N, Internet <address>, DNS lookup N), with the configured name beside it. A
// hop that ignores ping (no reply while a later hop answers: path.js quietTargets, from the live `targets` topic) gets no
// blocks, only the words Path health gives it: its strip would be one long outage that is none, and it neither counts as
// an outage nor opens incidents. So does an IPv6 target while this host has no IPv6 (its own words: "IPv6 unavailable on
// this host"). present() makes the view model every view draws (view.js is the standard one; docs/themes.md, "Drawing a
// card"); the window buttons and the header's note are the frame, kept here.

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { createCard } from '../../js/cards.js';
import { getTargetInfo, percentText, targetList, zonedFormat, zoneParams } from '../../js/util.js';
import { quietKey, quietTargets, shortLabels } from '../../js/path.js';
import standard from './view.js';

const CELL = { ok: '', degraded: 'w', outage: 'c', busy: 'b', no_data: 'n' };
const WORD = { ok: 'no loss', degraded: 'loss or a dead minute', outage: 'outage', busy: 'busy line (expected)', no_data: 'no data' };
const META = { '24h': '15-min blocks, path order', '7d': 'daily blocks, 7 days, path order', '30d': 'daily blocks, 30 days, path order' };
const LEGEND = [['ok', 'No loss'], ['degraded', 'Loss or a dead minute'], ['outage', 'Outage'], ['busy', 'Busy line (expected)'], ['no_data', 'No data']];
const LEGEND_NOTE = 'Path order: a problem that starts at one hop repeats in every row below it.';

let win = '24h';
let seq = 0;
let card = null;
let shown = null; // the last answer, drawn again when the hops that ignore ping change
let quiet = new Map(); // target id -> its neutral words (path.js quietTargets)
let lastKey = ''; // the view model drawn now; '' when the body shows something else (an error, nothing yet)

// Each block's start, end and words: the server's bounds (a day can have 23 or 25 hours), on its clock.
function blockSpans(data) {
  const clock = zonedFormat(data, { hour: '2-digit', minute: '2-digit' });
  const day = zonedFormat(data, { weekday: 'short', month: 'short', day: 'numeric' });
  return data.bounds.slice(0, -1).map((start, i) => {
    const end = data.bounds[i + 1];
    return { start, end, label: data.block_seconds < 86400 ? `${clock(start)}–${clock(end)}` : day(start) };
  });
}

function axis(data) {
  const n = 4;
  const at = data.block_seconds < 86400 ? zonedFormat(data, { hour: '2-digit', minute: '2-digit' }) : zonedFormat(data, { month: 'short', day: 'numeric' });
  return Array.from({ length: n + 1 }, (_, k) => (k === n ? 'now' : at(data.start + ((data.end - data.start) * k) / n)));
}

// The view model of an answer: { state: 'ready' | 'empty', blocks, columns, rows, axis, legend, meta } (a row: { id, short,
// name, sub, quiet: {why, strip, aria, pctTitle} | null, aria, pct, cells: [{ id, state, cls, start, end, label, tip, aria }] }).
// `ctx`: { names: id -> the configured name, labels, quiet }.
export function present(data, ctx, windowId) {
  const base = { columns: ['Target', '', 'Avail.'], legend: { items: LEGEND.map(([key, word]) => ({ key, word })), note: LEGEND_NOTE }, meta: META[windowId] };
  if (!data.targets.length) return { ...base, state: 'empty', empty: 'No probe data in this window yet.' };
  const spans = blockSpans(data);
  return {
    ...base,
    state: 'ready',
    blocks: data.blocks,
    rows: data.targets.map((t) => {
      const name = ctx.names(t.target_id) || t.name;
      const short = ctx.labels[t.target_id] || name;
      const row = { id: t.target_id, short, name, sub: short === name ? '' : name };
      if (ctx.quiet.has(t.target_id)) {
        const q = ctx.quiet.get(t.target_id);
        return { ...row, quiet: { why: q.why, strip: q.strip, aria: `${short} (${name}): ${q.strip}`, pctTitle: `${q.word}: not counted` }, pct: '–', cells: [] };
      }
      const bad = t.cells.filter((c) => c === 'outage').length;
      const warn = t.cells.filter((c) => c === 'degraded').length;
      const pct = percentText(t.availability_pct);
      return {
        ...row, quiet: null, pct,
        aria: `${short} (${name}): ${bad} outage blocks, ${warn} degraded blocks, ${pct} available`,
        cells: t.cells.map((c, i) => {
          const { start, end } = spans[i];
          const label = `${short} · ${spans[i].label}`;
          return { id: String(i), state: c, cls: CELL[c] ?? 'n', start, end, label,
            tip: `${short} · ${spans[i].label} · ${WORD[c] || c} · click for its incidents`, aria: `${label}, ${WORD[c] || c}: show the incidents` };
        })
      };
    }),
    axis: axis(data)
  };
}

// Made-up answer for the parity test of the card's views: a normal row, one that ignores ping, one with an outage.
export function fixture() {
  const start = 1767225600; // a fixed moment
  const bounds = Array.from({ length: 5 }, (_, i) => start + i * 900);
  const data = { blocks: 4, block_seconds: 900, start, end: bounds[4], bounds, zone: 'UTC', tz_offset_minutes: 0, targets: [
    { target_id: 'fx_router', name: 'Example router', cells: ['ok', 'ok', 'ok', 'ok'], availability_pct: 100 },
    { target_id: 'fx_hop', name: 'Example hop 2', cells: ['outage', 'outage', 'outage', 'outage'], availability_pct: 0 },
    { target_id: 'fx_dns', name: 'Example public DNS', cells: ['ok', 'degraded', 'outage', 'busy'], availability_pct: 87.5 }
  ] };
  const names = { fx_router: 'Example router', fx_hop: 'Example hop 2', fx_dns: 'Example public DNS' };
  return present(data, {
    names: (id) => names[id], labels: { fx_router: 'Router', fx_hop: 'ISP hop 2', fx_dns: 'Internet' },
    quiet: new Map([['fx_hop', { word: 'Ignores ping', why: 'Does not answer ping, but the hops after it do', strip: 'Ignores ping: the hops after it answer' }]])
  }, '24h');
}

// The answer changes once per block (15 minutes, or a day), the refresh comes every five: drawing 960 buttons for the
// same answer costs a 200 ms block of the page's one thread, so a view model like the one drawn is not handed on.
// Names and the hops that ignore ping are in the view model too.
function renderStrips(data) {
  shown = data;
  if (!card) return;
  const vm = present(data, { names: (id) => getTargetInfo(id).name, labels: shortLabels(targetList()), quiet }, win);
  const key = JSON.stringify(vm);
  if (key === lastKey) return;
  lastKey = key;
  card.update(vm);
}

async function load() {
  const mine = ++seq;
  document.getElementById('stripsMeta').textContent = META[win];
  document.querySelectorAll('#stripsRanges button').forEach((b) => {
    b.classList.toggle('active', b.dataset.arg === win);
    b.setAttribute('aria-pressed', String(b.dataset.arg === win));
  });
  try {
    const res = await fetch(`/api/uptime?window=${win}&${zoneParams()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (mine === seq) renderStrips(data);
  } catch (e) {
    if (mine !== seq) return;
    lastKey = '';
    card.update({ state: 'error', error: `Could not load the uptime: ${e.message}` });
  }
}

export function initStrips(section) {
  card = createCard({ panel: 'strips', root: section.querySelector('.panel-b'), standard, fixture });
  load();
  setInterval(load, 300000);
  bus.subscribe('targets', (targets) => {
    const next = quietTargets(targets);
    if (quietKey(next) === quietKey(quiet)) return;
    quiet = next;
    if (shown) renderStrips(shown);
  });
}

registerActions('click', {
  setUptimeWindow: (el) => { win = el.dataset.arg; load(); }
});
