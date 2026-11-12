// Reading the history of the target (or the members of the group) for the range, and drawing it: the charts, the 24 hour
// strips and the figures. A newer request (another range, target or view) owns the charts: older answers are dropped.

import { badge } from '../../js/dom.js';
import { escapeHtml } from '../../js/escape.js';
import { zoneParams } from '../../js/clock.js';
import { token } from '../../js/chart-theme.js';
import { median } from '../../js/values.js';
import { getTargetInfo } from '../../js/targets.js';
import { ROLE_WORD } from '../../js/roles.js';
import { fetchPathEvents } from '../../js/path-api.js';
import { liveQuiet, renderGroupUptime, renderUptime } from './strips.js';
import { lossBar, paintMarks, setRange, tint } from './charts.js';
import { lossOf, num, per, setStats, stateBadge, unit } from './figures.js';
import { RANGES, T, win } from './state.js';

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// `quiet`: the live refresh every 5 s. It does not set "Loading...", which a screen reader would announce each time.
export async function load(quiet = false) {
  const mine = ++T.seq;
  const status = document.getElementById('targetHistStatus');
  const [, , bucket] = RANGES.find((r) => r[0] === T.rangeSeconds);
  const end = Math.floor(Date.now() / 1000);
  if (!quiet) status.textContent = 'Loading...';
  const ids = T.group && T.view === 'all' ? T.group.members.map((m) => m.id) : [T.group ? T.view : T.targetId];
  const q = `${ids.map((i) => `target_ids=${encodeURIComponent(i)}`).join('&')}&start_time=${end - T.rangeSeconds}&end_time=${end}&bucket_seconds=${bucket}&compact=true`;
  const uptime = getJson(`/api/uptime?window=24h&${zoneParams()}`).catch(() => null);
  try {
    const { fields, rows } = await getJson(`/api/history?${q}`);
    if (mine !== T.seq) return; // a newer request (another range, target or view) owns the charts
    const col = (n) => fields.indexOf(n);
    const c = { id: col('target_id'), t: col('timestamp'), lat: col('latency_ms'), loss: col('packet_loss_pct'), p10: col('latency_p10_ms'),
      p90: col('latency_p90_ms'), ok: col('successful_count'), tot: col('total_count') };
    if (ids.length > 1) {
      paintMarks([]);
      await showGroup(mine, rows, c, { end, bucket, uptime });
    } else {
      await showOne(mine, ids[0], rows, c, { end, bucket, uptime, events: fetchPathEvents(ids[0]).catch(() => []) });
    }
  } catch (e) {
    if (mine === T.seq) status.textContent = `Could not load the history: ${e.message}`;
  }
}

async function showOne(mine, id, rows, c, { end, bucket, uptime, events = Promise.resolve([]) }) {
  const targets = getJson('/api/targets').catch(() => []);
  const info = getTargetInfo(id);
  const at = (r, k) => (k < 0 ? null : r[k]);
  const hasBand = c.p10 >= 0 && c.p90 >= 0 && rows.some((r) => num(r[c.p10]) && num(r[c.p90]));
  const sets = [{
    id: 'line', label: 'Latency (ms)', data: rows.map((r) => ({ x: r[c.t] * 1000, y: r[c.lat] })), borderColor: info.color,
    backgroundColor: info.color, borderWidth: 1.5, pointRadius: 0, tension: 0, spanGaps: false, order: 1
  }];
  if (hasBand) {
    sets.push({ id: 'band_lo', label: 'p10', data: rows.map((r) => ({ x: r[c.t] * 1000, y: r[c.p10] })), borderWidth: 0, pointRadius: 0, fill: false, spanGaps: false, order: 5 });
    sets.push({ id: 'band_hi', label: 'p10–p90', data: rows.map((r) => ({ x: r[c.t] * 1000, y: r[c.p90] })), borderWidth: 0, pointRadius: 0, backgroundColor: tint(info.color), fill: '-1', spanGaps: false, order: 5 });
  }
  T.latChart.data.datasets = sets;
  T.lossChart.data.datasets = [{
    label: 'Loss (%)', data: rows.map((r) => ({ x: r[c.t] * 1000, y: lossBar(r[c.loss]) })), borderColor: token('--crit'),
    backgroundColor: token('--crit'), barThickness: 3
  }];
  const changes = (await events).filter((e) => e && (e.kind === 'address_changed' || e.kind === 'probe_changed') && e.target_id === id);
  if (mine !== T.seq) return;
  const marks = changes.map((e) => ({ x: e.at * 1000, text: `${e.kind === 'probe_changed' ? 'Probe changed' : 'Address changed'}: ${e.old} → ${e.new}` }));
  paintMarks(marks);
  [T.latChart, T.lossChart].forEach((chart) => setRange(chart, end));
  document.getElementById('targetHistNote').textContent = hasBand ? 'p10 to p90 band, loss underneath' : 'average per bucket, loss underneath';
  document.getElementById('targetHistUpNote').textContent = '15-minute blocks';
  const lost = rows.reduce((a, r) => a + (r[c.loss] > 0 ? 1 : 0), 0);
  document.getElementById('targetHistStatus').textContent = rows.length
    ? `${rows.length} points, one per ${per(bucket)}; ${lost} with loss`
    : 'No samples stored for this range.';

  // The figures: this range's median, the highest p90 (or the highest average without a band) and its loss.
  const peaks = rows.map((r) => (hasBand ? at(r, c.p90) : r[c.lat])).filter(num);
  const [up, list] = await Promise.all([uptime, targets]);
  if (mine !== T.seq) return;
  const quiet = liveQuiet().get(id) || null; // its neutral words, or null
  const strip = renderUptime(up, id, info.name, quiet, marks);
  const mine1 = Array.isArray(list) ? list.find((x) => (x.target_id || x.id) === id) : null;
  const metaEl = document.getElementById('targetHistMeta');
  metaEl.textContent = mine1 ? `${ROLE_WORD[mine1.category] || mine1.category} · ${mine1.address}${mine1.probe_label ? ` · ${mine1.probe_label}` : ''}` : '';
  metaEl.title = mine1 && mine1.probe_note ? mine1.probe_note : '';
  setStats([
    quiet ? `<span title="${escapeHtml(quiet.why)}">${badge('unknown', quiet.word)}</span>` : stateBadge(strip ? strip.cells[strip.cells.length - 1] : null),
    unit(median(rows.map((r) => r[c.lat])), 'ms'),
    unit(peaks.length ? Math.max(...peaks) : null, 'ms'),
    quiet ? '–' : unit(lossOf(rows, c), '%', 2), // a neutral target: its loss counts nowhere, as on the path
    strip && num(strip.availability_pct) ? unit(strip.availability_pct, '%', 2) : '–'
  ]);
}

// The rank of an uptime block, worst last; no data says nothing either way.
const BLOCK_RANK = { ok: 0, busy: 1, degraded: 2, outage: 3 };

// A group's status from its members' latest blocks, by the node's rule: a median group is down only when every member
// is (degraded when any is not fine), a run of hops is as bad as its worst hop.
function groupState(rule, cells) {
  const known = cells.filter((cell) => cell in BLOCK_RANK);
  if (!known.length) return cells.length ? 'no_data' : null;
  const worst = known.reduce((a, b) => (BLOCK_RANK[b] > BLOCK_RANK[a] ? b : a));
  if (rule === 'median' && worst === 'outage' && known.some((cell) => cell !== 'outage')) return 'degraded';
  return worst;
}

// All members of a group: a line per member in its own color, the node's figure as a bold neutral line on top (the
// median per bucket, or the latency of the last hop that answers), loss per member, a strip per member, and the
// figures by the node's rules. Hops that ignore ping (path.js) stay out of the figures, as on the node.
async function showGroup(mine, rows, c, { end, bucket, uptime }) {
  const { rule, members } = T.group;
  const quiet = liveQuiet();
  const heardIds = members.map((m) => m.id).filter((id) => !quiet.has(id));
  const judged = heardIds.length ? heardIds : members.map((m) => m.id);
  const byId = new Map(members.map((m) => [m.id, []]));
  rows.forEach((r) => { if (byId.has(r[c.id])) byId.get(r[c.id]).push(r); });
  const at = new Map(members.map((m) => [m.id, new Map(byId.get(m.id).map((r) => [r[c.t], r[c.lat]]))]));
  const times = [...new Set(rows.map((r) => r[c.t]))].sort((a, b) => a - b);
  const figure = (ts) => (rule === 'median' ? median(members.map((m) => at.get(m.id).get(ts)))
    : ([...judged].reverse().map((id) => at.get(id).get(ts)).find(num) ?? null));
  const line = times.map((ts) => ({ x: ts * 1000, y: figure(ts) }));
  const ink = token('--ink-1');
  const figureName = rule === 'median' ? 'Median' : 'Last hop that answers';
  T.latChart.data.datasets = [
    ...members.map((m, k) => {
      const color = getTargetInfo(m.id).color;
      return { id: `member_${k}`, label: m.short, data: byId.get(m.id).map((r) => ({ x: r[c.t] * 1000, y: r[c.lat] })), borderColor: color,
        backgroundColor: color, borderWidth: 1.25, pointRadius: 0, tension: 0, spanGaps: false, order: 2 };
    }),
    { id: 'group', label: figureName, data: line, borderColor: ink, backgroundColor: ink, borderWidth: 2.75, pointRadius: 0, tension: 0, spanGaps: false, order: 0 }
  ];
  T.lossChart.data.datasets = members.filter((m) => judged.includes(m.id)).map((m) => {
    const color = getTargetInfo(m.id).color;
    return { label: m.short, data: byId.get(m.id).map((r) => ({ x: r[c.t] * 1000, y: lossBar(r[c.loss]) })), borderColor: color, backgroundColor: color, barThickness: 3 };
  });
  [T.latChart, T.lossChart].forEach((chart) => setRange(chart, end));
  const what = rule === 'last' ? 'hop' : T.group.category === 'dns' ? 'lookup' : 'server';
  document.getElementById('targetHistNote').textContent = rule === 'median'
    ? `a line per ${what}, the bold line their median (the figure on the node); loss per ${what} underneath`
    : 'a line per hop, the bold line the last hop that answers (the figure on the node); loss per hop underneath';
  document.getElementById('targetHistUpNote').textContent = `15-minute blocks, a row per ${what}`;
  document.getElementById('targetHistMeta').textContent = `${ROLE_WORD[T.group.category] || T.group.category} · ${members.length} ${what}s`;
  const lost = new Set(judged.flatMap((id) => byId.get(id).filter((r) => r[c.loss] > 0).map((r) => r[c.t]))).size;
  document.getElementById('targetHistStatus').textContent = rows.length
    ? `${times.length} points per line, one per ${per(bucket)}; ${lost} with loss`
    : 'No samples stored for this range.';

  // The figures, as the node computes them: the median of the members' own medians, or the last hop that answers; the
  // worst member's loss; the availability as the report takes it (the median of the members), or the worst hop's.
  const own = (id) => median(byId.get(id).map((r) => r[c.lat]));
  const rep = [...judged].reverse().find((id) => num(own(id)));
  const mid = rule === 'median' ? median(members.map((m) => own(m.id))) : (rep ? own(rep) : null);
  const peaks = line.map((p) => p.y).filter(num);
  const losses = judged.map((id) => lossOf(byId.get(id), c)).filter(num);
  const up = await uptime;
  if (mine !== T.seq) return;
  const strips = renderGroupUptime(up, members, quiet);
  const judgedStrips = judged.map((id) => strips.get(id)).filter(Boolean);
  const avail = judgedStrips.map((t) => t.availability_pct).filter(num);
  setStats([
    stateBadge(groupState(rule, judgedStrips.map((t) => t.cells[t.cells.length - 1]))),
    unit(mid, 'ms'),
    unit(peaks.length ? Math.max(...peaks) : null, 'ms'),
    unit(losses.length ? Math.max(...losses) : null, '%', 2),
    unit(rule === 'median' ? median(avail) : (avail.length ? Math.min(...avail) : null), '%', 2)
  ], ['Status, latest 15 min', `${rule === 'median' ? 'Median' : 'Median, last hop'}, ${win()}`, `Peak, ${figureName.toLowerCase()}, ${win()}`,
    `Loss, worst ${what}, ${win()}`, rule === 'median' ? 'Available, 24 h (median)' : 'Available, 24 h (worst hop)']);
}
