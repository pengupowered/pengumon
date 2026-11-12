// Target detail modal: one probe target's latency band (p10 to p90, where the server knows it), loss and 24 h uptime
// strip, over Live / 15 m / 1 h / 24 h / 7 d, from the server-side buckets of GET /api/history?target_ids=&bucket_seconds=
// and GET /api/uptime. Opened by clicking a hop, a matrix row or a strip's name. The modal is built on first use (so the
// page template stays small); fetched on open and on a range change, and every few seconds in the Live range.
//
// A path node of several targets (the Internet node, the median of the public servers; a folded run of ISP hops) opens
// the dialog on the group: a switcher shows All or one member. All draws a line per member in its own color and the
// node's figure as a bold neutral line, and the figures follow the node's rules (path.js, panels/diagnosis): the median
// of the members, or the last hop that answers; down only when every member is down, or as soon as any hop is.
//
// Where a target's address was replaced in the path editor (GET /api/path/events: same id, so the history goes on), a
// dashed marker on the latency and loss charts and on the 24 h strip says "Address changed: old → new", and a line
// under the charts lists it. A hop probed another way ("probe_changed": ping → TCP 443) is marked the same way.
//
// state.js holds what the dialog shows now, build.js its markup, charts.js the two charts, strips.js the 24 hour strips,
// figures.js the figures, load.js the reading and drawing of the history; this file is the dialog: range, group view, the
// Live refresh, the figure the path shows now, and the actions.

import { registerActions } from '../../js/dispatch.js';
import { escapeHtml } from '../../js/escape.js';
import { formatMs } from '../../js/format.js';
import { median } from '../../js/values.js';
import { getTargetInfo } from '../../js/targets.js';
import { openModal } from '../../js/modal.js';
import { bus } from '../../js/bus.js';
import { pathNodes } from '../../js/path.js';
import { ensureModal } from './build.js';
import { makeChart } from './charts.js';
import { num, setStats } from './figures.js';
import { load } from './load.js';
import { LIVE, RANGES, T, win } from './state.js';

let liveTimer = null;
let nodeKey = null; // the path node the dialog was opened from (panels/diagnosis): its figure is shown as it is now

function markRange() {
  document.querySelectorAll('#targetHistRanges button').forEach((b) => {
    const on = b.dataset.arg === String(T.rangeSeconds);
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

function keepLive() {
  clearInterval(liveTimer);
  liveTimer = null;
  if (T.rangeSeconds !== LIVE) return;
  liveTimer = setInterval(() => {
    const modal = document.getElementById('targetHistModal');
    if (!modal || !modal.classList.contains('active') || T.rangeSeconds !== LIVE) { clearInterval(liveTimer); liveTimer = null; return; }
    load(true);
  }, 5000);
}

// The title and the switcher: a group's title says what its node says; a member shown alone is titled by its name. The
// buttons are built when the dialog opens and only marked afterwards, so the one a keyboard pressed keeps the focus.
function paintView(rebuild = false) {
  const member = T.group && T.view !== 'all' ? T.group.members.find((m) => m.id === T.view) : null;
  const id = member ? member.id : T.targetId;
  const known = id && getTargetInfo(id).name !== id ? getTargetInfo(id).name : null;
  document.getElementById('targetHistTitle').textContent = T.group && !member ? T.group.title : (known || (member ? member.name : id));
  const box = document.getElementById('targetHistSwitch');
  box.hidden = !T.group;
  if (!T.group) { box.innerHTML = ''; return; }
  if (rebuild) {
    const button = (arg, label, title, color) => `<button type="button" data-action="setTargetHistoryView" data-arg="${escapeHtml(arg)}"`
      + `${title ? ` title="${escapeHtml(title)}"` : ''}>${color ? `<i class="key line" style="--c:${escapeHtml(color)}"></i>` : ''}${escapeHtml(label)}</button>`;
    box.innerHTML = button('all', 'All', T.group.title) + T.group.members.map((m) => button(m.id, m.short, m.name, getTargetInfo(m.id).color)).join('');
  }
  box.querySelectorAll('button').forEach((b) => {
    const on = b.dataset.arg === T.view;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

// The group a path node hands over (data-group, JSON): used only when it names at least two members.
function parseGroup(text) {
  if (!text) return null;
  try {
    const g = JSON.parse(text);
    const members = Array.isArray(g.members) ? g.members.filter((m) => m && typeof m.id === 'string' && m.id) : [];
    if (members.length < 2) return null;
    return { title: String(g.title || 'Targets'), rule: g.rule === 'last' ? 'last' : 'median', category: String(g.category || ''),
      members: members.map((m) => ({ id: m.id, short: String(m.short || m.name || m.id), name: String(m.name || m.id) })) };
  } catch (e) {
    return null;
  }
}

// The figure the path shows for what the dialog shows now, from the live topic: the node's own (pathNodes, the same
// function that paints it) for the node the dialog was opened from, a member's as a node of its own would show it
// (the median of its last 60 probes). Painted under the figures with the window those cover, so the number that was
// clicked can be found here. Only for a dialog opened from a path node.
function paintNow() {
  const el = document.getElementById('targetHistNow');
  const modal = document.getElementById('targetHistModal');
  if (!el || !modal || !modal.classList.contains('active')) return;
  const targets = bus.last('targets');
  el.hidden = !nodeKey || !Array.isArray(targets);
  if (el.hidden) return;
  const member = T.group && T.view !== 'all' ? T.view : null;
  let ms = null;
  let what = 'the median of its last 60 probes';
  if (member) {
    const t = targets.find((x) => x.target_id === member);
    const recent = t ? (t.recent_latencies || []).slice(-60) : [];
    ms = recent.length ? median(recent) : (t && num(t.last_ms) ? t.last_ms : null);
  } else {
    const node = pathNodes(targets, { collapseIsp: nodeKey === 'ISP:run' }).find((n) => n.key === nodeKey);
    ms = node ? node.ms : null;
    if (T.group) what = T.group.rule === 'median' ? `the median of the ${T.group.category === 'dns' ? 'lookups' : 'servers'}' last 60 probes` : 'the last hop that answers, the median of its last 60 probes';
  }
  const text = `${member ? 'This target' : 'On the path'} now: ${num(ms) ? `${formatMs(ms, { unit: true })}` : 'no reply'} (${what}). The figures above cover the ${win()}.`;
  if (el.textContent !== text) el.textContent = text;
}
bus.subscribe('targets', paintNow);

// `from`: {range: 'live' to open on the Live range, node: the key of the path node clicked} (a path node passes both).
export function openTargetHistory(id, groupSpec = null, from = {}) {
  const g = typeof groupSpec === 'string' ? parseGroup(groupSpec) : groupSpec;
  if (!id && !g) return;
  T.group = g;
  T.view = 'all';
  T.targetId = g ? null : id;
  nodeKey = from.node || null;
  if (from.range === 'live') T.rangeSeconds = LIVE; // the path's figures are of the last minute: the closest range
  ensureModal();
  markRange();
  openModal('targetHistModal'); // shown before the charts exist, so they are sized by the visible canvas
  paintView(true);
  document.getElementById('targetHistMeta').textContent = '';
  setStats(['–', '–', '–', '–', '–']);
  if (!T.latChart) {
    T.latChart = makeChart('targetHistLatency', 'line', 'Latency (ms)', { suggestedMax: 30 });
    T.lossChart = makeChart('targetHistLoss', 'bar', 'Loss (%)', { max: 100 });
    T.latChart.options.scales.x.ticks.display = false; // one time axis, under the loss bars
  }
  keepLive();
  load();
  paintNow();
}

// The switcher of a group: All, or one member as a target of its own.
export function setTargetHistoryView(arg) {
  if (!T.group || !(arg === 'all' || T.group.members.some((m) => m.id === arg))) return;
  T.view = arg;
  paintView();
  document.getElementById('targetHistMeta').textContent = '';
  setStats(['–', '–', '–', '–', '–']);
  load();
  paintNow();
}

export function setTargetHistoryRange(seconds) {
  if (!RANGES.some((r) => r[0] === seconds) || !(T.targetId || T.group)) return;
  T.rangeSeconds = seconds;
  markRange();
  keepLive();
  setStats(['–', '–', '–', '–', '–']);
  load();
  paintNow();
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openTargetHistory: (el) => openTargetHistory(el.dataset.arg, el.dataset.group, { range: el.dataset.range, node: el.dataset.node }),
  setTargetHistoryView: (el) => setTargetHistoryView(el.dataset.arg),
  setTargetHistoryRange: (el) => setTargetHistoryRange(Number(el.dataset.arg))
});
