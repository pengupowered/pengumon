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
// under the charts lists it.

import { registerActions } from '../../js/dispatch.js';
import { badge, escapeHtml, formatClock, formatMs, getTargetInfo, icon, keepKeyboard, localFormat, median, openModal, zonedFormat, zoneParams } from '../../js/util.js';
import { token } from '../../js/chart-theme.js';
import { bus } from '../../js/bus.js';
import { pathNodes, quietTargets } from '../../js/path.js';
import { ROLE_WORD, fetchPathEvents } from '../../js/path-api.js';

const RANGES = [[300, 'Live', 2], [900, '15 m', 10], [3600, '1 h', 30], [86400, '24 h', 600], [604800, '7 d', 3600]]; // seconds, label, bucket seconds
const LIVE = 300;
const CELL = { ok: '', degraded: 'w', outage: 'c', busy: 'b', no_data: 'n' };
const WORD = { ok: 'no loss', degraded: 'loss or a dead minute', outage: 'outage', busy: 'busy line (expected)', no_data: 'no data' };
const LEVEL = { ok: 'good', degraded: 'warn', outage: 'crit', busy: 'busy', no_data: 'unknown' };
const STATE_WORD = { ok: 'Healthy', degraded: 'Degraded', outage: 'Down', busy: 'Busy', no_data: 'No data' };
// What each range's figures cover, said in their labels (a figure without its window cannot be compared with the path's).
const WINDOW = { 300: 'last 5 min', 900: 'last 15 min', 3600: 'last hour', 86400: 'last 24 h', 604800: 'last 7 days' };
const win = () => WINDOW[rangeSeconds];
const labels = () => ['Status, latest 15 min', `Median, ${win()}`, `p90 peak, ${win()}`, `Loss, ${win()}`, 'Available, 24 h'];
let liveTimer = null;
let nodeKey = null; // the path node the dialog was opened from (panels/diagnosis): its figure is shown as it is now

let targetId = null; // the target of a single-target dialog
let group = null; // {title, rule: 'median' | 'last', category, members: [{id, short, name}]} of a dialog opened on a group
let view = 'all'; // with a group: 'all' or the id of the member shown
let rangeSeconds = 3600;
let seq = 0;
let latChart = null;
let lossChart = null;

function make(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function ensureModal() {
  let modal = document.getElementById('targetHistModal');
  if (modal) return modal;
  modal = make('div', 'modal');
  modal.id = 'targetHistModal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'targetHistTitle');
  const content = make('div', 'modal-content');
  content.style.setProperty('--w', '940px');
  const head = make('header', 'modal-h');
  head.innerHTML = icon('globe');
  const h2 = head.appendChild(make('h2', 'modal-t', 'Target history'));
  h2.id = 'targetHistTitle';
  h2.tabIndex = -1;
  head.appendChild(make('span', 'modal-m')).id = 'targetHistMeta';
  const tools = head.appendChild(make('div', 'tools'));
  const ranges = tools.appendChild(make('span', 'seg'));
  ranges.setAttribute('role', 'group');
  ranges.setAttribute('aria-label', 'Range');
  ranges.id = 'targetHistRanges';
  RANGES.forEach(([seconds, label]) => {
    const b = make('button', '', label);
    b.type = 'button';
    b.dataset.action = 'setTargetHistoryRange';
    b.dataset.arg = String(seconds);
    ranges.appendChild(b);
  });
  const close = make('button', 'btn icon m-close');
  close.innerHTML = icon('close');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  close.dataset.action = 'closeModal';
  close.dataset.arg = 'targetHistModal';
  head.append(close);
  const body = make('div', 'modal-b');
  const sw = body.appendChild(make('div', 'seg tgt-switch'));
  sw.id = 'targetHistSwitch';
  sw.setAttribute('role', 'group');
  sw.setAttribute('aria-label', 'Show');
  sw.hidden = true;
  const stats = body.appendChild(make('div', 'tgt-stats'));
  stats.id = 'targetHistStats';
  stats.innerHTML = labels().map((l) => `<div class="stat"><span class="lbl">${l}</span><span class="val num">–</span></div>`).join('');
  const now = body.appendChild(make('p', 'fine tgt-now'));
  now.id = 'targetHistNow';
  now.hidden = true;
  const latSect = body.appendChild(make('section', 'sect'));
  latSect.innerHTML = `<h3 class="section-t">${icon('activity')}Latency<span class="muted" id="targetHistNote">p10 to p90 band, loss underneath</span></h3>`;
  const latBox = latSect.appendChild(make('div', 'chart-box'));
  latBox.style.setProperty('--h', '210px');
  const lat = latBox.appendChild(document.createElement('canvas'));
  lat.id = 'targetHistLatency';
  lat.setAttribute('role', 'img');
  lat.setAttribute('aria-label', 'Latency over the chosen range');
  const lossBox = latSect.appendChild(make('div', 'chart-box tgt-loss'));
  lossBox.style.setProperty('--h', '80px');
  const loss = lossBox.appendChild(document.createElement('canvas'));
  loss.id = 'targetHistLoss';
  loss.setAttribute('role', 'img');
  loss.setAttribute('aria-label', 'Lost probes over the chosen range');
  const status = latSect.appendChild(make('p', 'fine'));
  status.id = 'targetHistStatus';
  status.setAttribute('role', 'status');
  const events = latSect.appendChild(make('ul', 'tgt-events'));
  events.id = 'targetHistEvents';
  events.setAttribute('aria-label', 'Changes to this target');
  events.hidden = true;
  const upSect = body.appendChild(make('section', 'sect'));
  upSect.innerHTML = `<h3 class="section-t">${icon('grid')}Last 24 hours<span class="muted" id="targetHistUpNote">15-minute blocks</span></h3><div id="targetHistStrip"></div>`;
  const foot = make('footer', 'modal-f');
  foot.innerHTML = '<button type="button" class="btn primary" data-action="closeModal" data-arg="targetHistModal">Done</button>';
  content.append(head, body, foot);
  modal.appendChild(content);
  document.body.appendChild(modal);
  keepKeyboard(modal, '.modal-t');
  return modal;
}

const clock = (ms) => (rangeSeconds === LIVE ? formatClock(ms, false, true) : formatClock(ms, rangeSeconds > 86400));

// The address changes of the target shown (chart.pgMarks: [{x: ms, text}]): a dashed line each, labeled on the latency
// chart (chart.pgMarkLabels).
const MARKS = {
  id: 'tgtMarks',
  afterDatasetsDraw(chart) {
    const marks = chart.pgMarks || [];
    const { ctx, chartArea: area, scales: { x } } = chart;
    if (!marks.length || !area || !x) return;
    ctx.save();
    ctx.strokeStyle = token('--warn');
    ctx.fillStyle = token('--ink-2');
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.font = '10px system-ui, sans-serif';
    marks.forEach((m) => {
      if (m.x < x.min || m.x > x.max) return;
      const px = x.getPixelForValue(m.x);
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.bottom);
      ctx.stroke();
      if (chart.pgMarkLabels) {
        const w = ctx.measureText('Address changed').width;
        ctx.fillText('Address changed', Math.min(px + 4, area.right - w), area.top + 10);
      }
    });
    ctx.restore();
  }
};

function makeChart(id, type, yTitle, yExtra) {
  return new Chart(document.getElementById(id), {
    type,
    plugins: [MARKS],
    data: { datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      scales: {
        x: { type: 'linear', grid: { display: false }, border: { color: () => token('--line-strong') }, ticks: { maxTicksLimit: 8, font: { size: 10 }, callback: (v) => clock(v) } },
        y: { beginAtZero: true, afterFit: (scale) => { scale.width = 56; }, ...yExtra, title: { display: true, text: yTitle, font: { size: 10 } }, grid: { color: () => token('--line') }, border: { display: false }, ticks: { font: { size: 10 } } }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => !String(item.dataset.id || '').startsWith('band_'),
          callbacks: { title: (items) => (items.length ? clock(items[0].parsed.x) : '') }
        }
      }
    }
  });
}

function markRange() {
  document.querySelectorAll('#targetHistRanges button').forEach((b) => {
    const on = b.dataset.arg === String(rangeSeconds);
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

const num = (v) => typeof v === 'number' && Number.isFinite(v);
const fmt = (v, digits = 1) => (num(v) ? v.toFixed(digits) : '–');

function setStats(cells, names = labels()) {
  document.querySelectorAll('#targetHistStats .stat').forEach((el, i) => {
    el.querySelector('.lbl').textContent = names[i];
    el.querySelector('.val').innerHTML = cells[i];
  });
}
const unit = (v, u, digits = 1) => (num(v) ? `${fmt(v, digits)}<small>${u}</small>` : '–');
const stateBadge = (cell) => (cell ? badge(LEVEL[cell] || 'unknown', STATE_WORD[cell] || 'No data') : '–');

// Block times on the clock the server cut the blocks on (their bounds and zone: /api/uptime), one formatter per strip.
const stripClock = (data) => zonedFormat(data, { hour: '2-digit', minute: '2-digit' });
function stripCells(data, t, name) {
  const hm = stripClock(data);
  const at = (i) => (data.bounds ? data.bounds[i] : data.start + i * data.block_seconds);
  return t.cells.map((c, i) => `<i class="${CELL[c] ?? 'n'}" data-tip="${escapeHtml(name)} · ${hm(at(i))}–${hm(at(i + 1))} · ${WORD[c] || c}"></i>`).join('');
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
const liveQuiet = () => quietTargets(bus.last('targets') || []);

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

function renderUptime(data, id, name, quiet, marks = []) {
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
function renderGroupUptime(data, members, quiet) {
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

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function setRange(c, end) {
  c.options.scales.x.min = (end - rangeSeconds) * 1000;
  c.options.scales.x.max = end * 1000;
  c.update();
}

// A bucket's loss as a bar: none where nothing was lost (a 0 bar still drew a faint dash along the axis).
const lossBar = (v) => (num(v) && v > 0 ? v : null);
const per = (bucket) => (bucket >= 60 ? `${bucket / 60} min` : `${bucket} s`);

// The figures of a list of history rows: sent and answered probes give the loss (the bucket average without counts).
function lossOf(rows, c) {
  const sent = c.tot >= 0 ? rows.reduce((a, r) => a + (r[c.tot] || 0), 0) : 0;
  const got = c.ok >= 0 ? rows.reduce((a, r) => a + (r[c.ok] || 0), 0) : 0;
  return sent > 0 ? ((sent - got) * 100) / sent : (rows.length ? rows.reduce((a, r) => a + (r[c.loss] || 0), 0) / rows.length : null);
}

async function load() {
  const mine = ++seq;
  const status = document.getElementById('targetHistStatus');
  const [, , bucket] = RANGES.find((r) => r[0] === rangeSeconds);
  const end = Math.floor(Date.now() / 1000);
  status.textContent = 'Loading...';
  const ids = group && view === 'all' ? group.members.map((m) => m.id) : [group ? view : targetId];
  const q = `${ids.map((i) => `target_ids=${encodeURIComponent(i)}`).join('&')}&start_time=${end - rangeSeconds}&end_time=${end}&bucket_seconds=${bucket}&compact=true`;
  const uptime = getJson(`/api/uptime?window=24h&${zoneParams()}`).catch(() => null);
  try {
    const { fields, rows } = await getJson(`/api/history?${q}`);
    if (mine !== seq) return; // a newer request (another range, target or view) owns the charts
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
    if (mine === seq) status.textContent = `Could not load the history: ${e.message}`;
  }
}

// One target: its latency with the p10-p90 band, its loss, its strip and figures (also a group's member on its own).
// The address changes of a target, as chart marks and a list under the charts; [] clears them.
function paintMarks(marks) {
  latChart.pgMarks = marks;
  latChart.pgMarkLabels = true;
  lossChart.pgMarks = marks;
  const list = document.getElementById('targetHistEvents');
  list.hidden = !marks.length;
  list.innerHTML = marks.map((m) => `<li>${icon('warn', 'sm')}<span>${escapeHtml(m.text)}</span> <span class="muted">${escapeHtml(localFormat(m.x, { dateStyle: 'medium', timeStyle: 'short' }))}</span></li>`).join('');
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
    sets.push({ id: 'band_hi', label: 'p10–p90', data: rows.map((r) => ({ x: r[c.t] * 1000, y: r[c.p90] })), borderWidth: 0, pointRadius: 0, backgroundColor: `${info.color}33`, fill: '-1', spanGaps: false, order: 5 });
  }
  latChart.data.datasets = sets;
  lossChart.data.datasets = [{
    label: 'Loss (%)', data: rows.map((r) => ({ x: r[c.t] * 1000, y: lossBar(r[c.loss]) })), borderColor: token('--crit'),
    backgroundColor: token('--crit'), barThickness: 3
  }];
  const changes = (await events).filter((e) => e && e.kind === 'address_changed' && e.target_id === id);
  if (mine !== seq) return;
  const marks = changes.map((e) => ({ x: e.at * 1000, text: `Address changed: ${e.old} → ${e.new}` }));
  paintMarks(marks);
  [latChart, lossChart].forEach((chart) => setRange(chart, end));
  document.getElementById('targetHistNote').textContent = hasBand ? 'p10 to p90 band, loss underneath' : 'average per bucket, loss underneath';
  document.getElementById('targetHistUpNote').textContent = '15-minute blocks';
  const lost = rows.reduce((a, r) => a + (r[c.loss] > 0 ? 1 : 0), 0);
  document.getElementById('targetHistStatus').textContent = rows.length
    ? `${rows.length} points, one per ${per(bucket)}; ${lost} with loss`
    : 'No samples stored for this range.';

  // The figures: this range's median, the highest p90 (or the highest average without a band) and its loss.
  const peaks = rows.map((r) => (hasBand ? at(r, c.p90) : r[c.lat])).filter(num);
  const [up, list] = await Promise.all([uptime, targets]);
  if (mine !== seq) return;
  const quiet = liveQuiet().get(id) || null; // its neutral words, or null
  const strip = renderUptime(up, id, info.name, quiet, marks);
  const mine1 = Array.isArray(list) ? list.find((x) => (x.target_id || x.id) === id) : null;
  document.getElementById('targetHistMeta').textContent = mine1 ? `${ROLE_WORD[mine1.category] || mine1.category} · ${mine1.address}` : '';
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
  const { rule, members } = group;
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
  latChart.data.datasets = [
    ...members.map((m, k) => {
      const color = getTargetInfo(m.id).color;
      return { id: `member_${k}`, label: m.short, data: byId.get(m.id).map((r) => ({ x: r[c.t] * 1000, y: r[c.lat] })), borderColor: color,
        backgroundColor: color, borderWidth: 1.25, pointRadius: 0, tension: 0, spanGaps: false, order: 2 };
    }),
    { id: 'group', label: figureName, data: line, borderColor: ink, backgroundColor: ink, borderWidth: 2.75, pointRadius: 0, tension: 0, spanGaps: false, order: 0 }
  ];
  lossChart.data.datasets = members.filter((m) => judged.includes(m.id)).map((m) => {
    const color = getTargetInfo(m.id).color;
    return { label: m.short, data: byId.get(m.id).map((r) => ({ x: r[c.t] * 1000, y: lossBar(r[c.loss]) })), borderColor: color, backgroundColor: color, barThickness: 3 };
  });
  [latChart, lossChart].forEach((chart) => setRange(chart, end));
  const what = rule === 'last' ? 'hop' : group.category === 'dns' ? 'lookup' : 'server';
  document.getElementById('targetHistNote').textContent = rule === 'median'
    ? `a line per ${what}, the bold line their median (the figure on the node); loss per ${what} underneath`
    : 'a line per hop, the bold line the last hop that answers (the figure on the node); loss per hop underneath';
  document.getElementById('targetHistUpNote').textContent = `15-minute blocks, a row per ${what}`;
  document.getElementById('targetHistMeta').textContent = `${ROLE_WORD[group.category] || group.category} · ${members.length} ${what}s`;
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
  if (mine !== seq) return;
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

function keepLive() {
  clearInterval(liveTimer);
  liveTimer = null;
  if (rangeSeconds !== LIVE) return;
  liveTimer = setInterval(() => {
    const modal = document.getElementById('targetHistModal');
    if (!modal || !modal.classList.contains('active') || rangeSeconds !== LIVE) { clearInterval(liveTimer); liveTimer = null; return; }
    load();
  }, 5000);
}

// The title and the switcher: a group's title says what its node says; a member shown alone is titled by its name. The
// buttons are built when the dialog opens and only marked afterwards, so the one a keyboard pressed keeps the focus.
function paintView(rebuild = false) {
  const member = group && view !== 'all' ? group.members.find((m) => m.id === view) : null;
  const id = member ? member.id : targetId;
  const known = id && getTargetInfo(id).name !== id ? getTargetInfo(id).name : null;
  document.getElementById('targetHistTitle').textContent = group && !member ? group.title : (known || (member ? member.name : id));
  const box = document.getElementById('targetHistSwitch');
  box.hidden = !group;
  if (!group) { box.innerHTML = ''; return; }
  if (rebuild) {
    const button = (arg, label, title, color) => `<button type="button" data-action="setTargetHistoryView" data-arg="${escapeHtml(arg)}"`
      + `${title ? ` title="${escapeHtml(title)}"` : ''}>${color ? `<i class="key line" style="--c:${escapeHtml(color)}"></i>` : ''}${escapeHtml(label)}</button>`;
    box.innerHTML = button('all', 'All', group.title) + group.members.map((m) => button(m.id, m.short, m.name, getTargetInfo(m.id).color)).join('');
  }
  box.querySelectorAll('button').forEach((b) => {
    const on = b.dataset.arg === view;
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
  const member = group && view !== 'all' ? view : null;
  let ms = null;
  let what = 'the median of its last 60 probes';
  if (member) {
    const t = targets.find((x) => x.target_id === member);
    const recent = t ? (t.recent_latencies || []).slice(-60) : [];
    ms = recent.length ? median(recent) : (t && num(t.last_ms) ? t.last_ms : null);
  } else {
    const node = pathNodes(targets, { collapseIsp: nodeKey === 'ISP:run' }).find((n) => n.key === nodeKey);
    ms = node ? node.ms : null;
    if (group) what = group.rule === 'median' ? `the median of the ${group.category === 'dns' ? 'lookups' : 'servers'}' last 60 probes` : 'the last hop that answers, the median of its last 60 probes';
  }
  const text = `${member ? 'This target' : 'On the path'} now: ${num(ms) ? `${formatMs(ms, { unit: true })}` : 'no reply'} (${what}). The figures above cover the ${win()}.`;
  if (el.textContent !== text) el.textContent = text;
}
bus.subscribe('targets', paintNow);

// `from`: {range: 'live' to open on the Live range, node: the key of the path node clicked} (a path node passes both).
export function openTargetHistory(id, groupSpec = null, from = {}) {
  const g = typeof groupSpec === 'string' ? parseGroup(groupSpec) : groupSpec;
  if (!id && !g) return;
  group = g;
  view = 'all';
  targetId = g ? null : id;
  nodeKey = from.node || null;
  if (from.range === 'live') rangeSeconds = LIVE; // the path's figures are of the last minute: the closest range
  ensureModal();
  markRange();
  openModal('targetHistModal'); // shown before the charts exist, so they are sized by the visible canvas
  paintView(true);
  document.getElementById('targetHistMeta').textContent = '';
  setStats(['–', '–', '–', '–', '–']);
  if (!latChart) {
    latChart = makeChart('targetHistLatency', 'line', 'Latency (ms)', { suggestedMax: 30 });
    lossChart = makeChart('targetHistLoss', 'bar', 'Loss (%)', { max: 100 });
    latChart.options.scales.x.ticks.display = false; // one time axis, under the loss bars
  }
  keepLive();
  load();
  paintNow();
}

// The switcher of a group: All, or one member as a target of its own.
export function setTargetHistoryView(arg) {
  if (!group || !(arg === 'all' || group.members.some((m) => m.id === arg))) return;
  view = arg;
  paintView();
  document.getElementById('targetHistMeta').textContent = '';
  setStats(['–', '–', '–', '–', '–']);
  load();
  paintNow();
}

export function setTargetHistoryRange(seconds) {
  if (!RANGES.some((r) => r[0] === seconds) || !(targetId || group)) return;
  rangeSeconds = seconds;
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
