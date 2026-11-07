// Path health, the model: the classifier's verdict for the whole line (the `diagnosis` topic) and the path from this
// host to the internet, hop by hop (the `targets` topic), turned into the view model every view of the card draws
// (view.js is the standard one; docs/themes.md, "Drawing a card"). Also the header's status pill, the page tint and
// the tab title, which follow the verdict, and the card's own status (its frame). Until the first message the card says
// "Connecting" in grey and the hops are skeletons, never a green "healthy".
//
// The chain follows the targets' categories in path order: this host, the local network (router or mesh), the gateway
// (modem / line), every ISP hop, then the internet (the public targets as one node; DNS lookups stand in when there
// is no public target). A category with several targets shows its worst one and the count. Each node: the median of
// the last probes (big number), loss and jitter, and the last 60 probes as a sparkline; each link: how much latency
// that segment adds. A node opens that target's history; the Internet node and a folded run of ISP hops open the history
// of all their targets together (a switcher there shows one of them).
//
// The view model: { verdict, banner, path, busyLegend }. A part is a new object only when it changed (a view compares
// them by identity): `verdict` on a diagnosis message, `path` on a targets message, `banner` when the first-run or
// route-change notice changes.

import * as util from '../../js/util.js';
import { createCard, kit } from '../../js/cards.js';
import { registerActions } from '../../js/dispatch.js';
import { pathNodes } from '../../js/path.js';
import { deviceLabel, deviceStatus, dismissDrift, fetchDrift, fetchPath, startDiscovery, waitDiscovery } from '../../js/path-api.js';
import { state } from '../../js/state.js';
import { bus as pageBus } from '../../js/bus.js';
import standard from './view.js';

const { escapeHtml, formatMs: fmtMs, latestMs, setClass, setText, STATUS } = util;

// The classifier's status -> the design's status level and its word.
const LEVEL = { healthy: 'good', degraded: 'warn', down: 'crit', busy: 'busy', unknown: 'unknown' };
const WORD = { healthy: 'Healthy', degraded: 'Degraded', down: 'Down', busy: 'Busy', unknown: 'Not probing' }; // unknown: this host cannot send pings

export const levelOf = (status) => LEVEL[status] || 'unknown';

const has = (v) => v !== null && v !== undefined;
const fmtPct = (v) => (v >= 100 ? '100%' : `${(+v).toFixed(1)}%`);

// ---------- the verdict ----------
// What the card says before the first message (panel.html says the same).
const CONNECTING = { level: 'unknown', word: 'Connecting', title: 'Analyzing network hops...', text: 'Starting the probes to your router, the gateway and the public resolvers.', inspect: null };

function verdictText(diagnosis) {
  if (diagnosis.status !== 'busy') return diagnosis.description;
  const load = diagnosis.load_pct !== null && diagnosis.load_pct !== undefined ? ` (${Math.round(diagnosis.load_pct)}% of capacity)` : '';
  return `Line busy - latency is expected under heavy load${load}`;
}

// The verdict as the views draw it: its status, title and sentence, and Inspect while an incident is open.
export function presentVerdict(diagnosis) {
  const id = diagnosis.active_incident && diagnosis.active_incident.id;
  return {
    level: levelOf(diagnosis.status), word: WORD[diagnosis.status] || diagnosis.status, title: diagnosis.title, text: verdictText(diagnosis),
    inspect: id ? { action: 'inspectIncident', arg: String(id), label: 'Inspect', icon: 'inspect' } : null
  };
}

// ---------- the chain (its nodes: path.js pathNodes) ----------
const STATE = { up: 'good', degraded: 'warn', down: 'crit' };

// The latency a segment adds, as the link's pill. A later hop that answers faster than the one before it (d < 0) is not
// a fault: routers de-prioritise replies to pings, and a target set as a later hop may be an anycast server reached by
// another route. A small such value reads as "about 0"; a large one is shown as it is, with the same explanation.
export const NEGATIVE_NOISE_MS = 10;
export function deltaText(d) {
  if (d >= 0) return { text: `+${d.toFixed(1)} ms`, title: 'Latency this segment adds' };
  const why = 'This hop answered faster than the one before it, so the segment adds about nothing. That is normal, not a fault: '
    + 'routers answer pings at low priority, and a target set as a later hop can be a nearby anycast server.';
  return { text: d > -NEGATIVE_NOISE_MS ? '≈0 ms' : `−${Math.abs(d).toFixed(1)} ms`, title: why };
}

// The device linked to each hop (the path editor's device line), for a node's tooltip: "Readings from: <label> ·
// Healthy · CPU 12%", from the device's latest reading (js/device-feed.js, the device cards' feed).
const hopDevices = new Map();
const NODE_ROLE = { Router: 'local', Gateway: 'gateway' };
function deviceTip(n, base, targets) {
  // the Router and Gateway nodes stand for every hop of their role; the others for their own target
  const members = NODE_ROLE[n.kind] ? targets.filter((t) => t.category === NODE_ROLE[n.kind]).map((t) => t.target_id) : [n.target];
  const ids = [...new Set(members.map((m) => hopDevices.get(m)).filter(Boolean))];
  const lines = ids.map((id) => state.devices.find((d) => d.id === id)).filter(Boolean).map((device) => {
    const st = deviceStatus(device, pageBus.last(`device:${device.id}`));
    return `Readings from: ${[deviceLabel(device), st.word || 'waiting', ...st.figures].join(' · ')}`;
  });
  return [base, ...lines].join('\n');
}

// pathNodes' nodes with what the views show, formatted: the label, the status word, the big value, loss and jitter (or
// why the hop is quiet), and the link before each node (what that segment adds, from the last hop before it that answers).
function decorate(nodes, targets) {
  let heard = null;
  return nodes.map((n) => {
    const word = n.quiet ? n.quietWords.word : STATUS[n.level].word; // ignores ping, or IPv6 unavailable on this host
    const unprobed = n.quiet && n.quietWords.kind !== 'ping'; // its last figures are from before IPv6 went away, or pings stopped
    const broken = !has(n.ms) && !n.quiet;
    let link;
    if (n.quiet) link = { text: '–', title: n.quietWords.why };
    else if (broken) link = { text: 'no reply', title: 'No answer from this hop' };
    else if (!heard || !has(heard.ms)) link = { text: `${n.ms.toFixed(1)} ms`, title: 'Latency this segment adds' };
    else link = deltaText(n.ms - heard.ms);
    if (has(n.ms)) heard = n;
    return {
      key: n.key, kind: n.kind, icon: n.icon, target: n.target, group: n.history ? JSON.stringify(n.history) : '',
      level: n.level, word, statusIcon: STATUS[n.level].icon, alert: n.level !== 'good' && !n.quiet,
      label: n.count > 1 ? `${n.label} · ${n.count}` : n.label,
      name: n.name, nameTitle: deviceTip(n, n.title || n.name, targets),
      aria: `${n.label}: ${n.name}, ${word}${has(n.ms) && !unprobed ? `, ${n.ms.toFixed(1)} milliseconds` : ''}. Show its history`,
      value: unprobed ? { word: 'Not probed' } : has(n.ms) ? { num: fmtMs(n.ms), unit: 'ms' } : { word: 'No reply' },
      quiet: n.quiet ? { text: n.quietWords.meta, why: n.quietWords.why } : null,
      loss: n.quiet ? null : { text: fmtPct(n.loss), bad: n.loss > 0 },
      jitter: !n.quiet && has(n.jitter) && has(n.ms) ? (+n.jitter).toFixed(1) : null,
      spark: unprobed ? [] : n.spark,
      link: { ...link, level: n.level, broken }
    };
  });
}

// The targets that are no stage of the path (category custom: a NAS, a VPN, a server): one chip each under the chain.
function extras(live) {
  return live.filter((t) => t.category === 'custom').map((t) => {
    const level = STATE[t.status] || 'unknown';
    return {
      id: t.target_id, level, icon: STATUS[level].icon, name: t.name,
      title: `${t.name} · ${t.address} · ${STATUS[level].word}. Show its history`,
      ms: has(latestMs(t)) ? `${fmtMs(latestMs(t))} ms` : 'no reply'
    };
  });
}

// The path as the views draw it: every node (`nodes`), the same with the ISP hops folded into one (`folded()`, for a
// view too narrow for a node per hop), the other targets, and the host's count of targets.
export function presentPath(targets, raw = pathNodes(targets)) {
  const live = targets.filter((t) => t.enabled !== false);
  const nodes = decorate(raw, live);
  let folded = null;
  return {
    hostCount: targets.length,
    nodes,
    folded: () => (folded ||= decorate(pathNodes(targets, { collapseIsp: true }), live)),
    none: 'No router, modem, provider hop or public server is a target, so there is no path to draw. Add one as a target to see it here.',
    extras: extras(live)
  };
}

// ---------- the header (page chrome) and the card's frame ----------
// Written only where something changed: the verdict comes every second, and the pill is a live region (a screen reader
// would read a rewritten one again).
function updateHeader(diagnosis, level) {
  const pill = document.getElementById('statusPill');
  setClass(pill, `status-pill st-${level}`);
  setText(document.getElementById('statusPillText'), diagnosis.title);
  if (pill && pill.title !== diagnosis.title) pill.title = diagnosis.title; // the pill cuts a long title with an ellipsis
  setText(document.getElementById('statusPillWord'), WORD[diagnosis.status] || diagnosis.title); // the phone's pill: one word
  setClass(document.getElementById('topbar'), `topbar st-${level}`);
  if (document.body.dataset.status !== level) document.body.dataset.status = level;
  const title = level === 'good' ? 'pengumon' : `${WORD[diagnosis.status]} · pengumon`;
  if (document.title !== title) document.title = title;
}

function frame(level) {
  const article = document.getElementById('diagnosisBanner');
  if (article.dataset.status === level && article.classList.contains(`st-${level}`)) return;
  article.dataset.status = level;
  article.classList.remove('st-good', 'st-warn', 'st-crit', 'st-busy', 'st-unknown');
  article.classList.add(`st-${level}`);
}

// The header pill: the worst loss on the path (hops that only ignore ping left out) and the internet's latency.
function pillMetrics(targets, nodes) {
  const internet = nodes.find((n) => n.kind === 'Internet') || nodes[nodes.length - 1]; // the first Internet node, not its IPv6 servers
  const worst = nodes.filter((n) => n.loss !== null).reduce((a, b) => (b.loss > (a ? a.loss : -1) ? b : a), null);
  const parts = [];
  // While this host can't send pings, the last figures are stale: the pill says nothing rather than old numbers.
  const noPings = targets.some((t) => t.probing_unavailable === true);
  if (!noPings && worst && worst.loss > 0) parts.push(`${fmtPct(worst.loss)} loss`);
  if (!noPings && internet && has(internet.ms)) parts.push(`${fmtMs(internet.ms)} ms to internet`);
  kit.html(document.getElementById('statusPillMetrics'), parts.map((p) => `<span class="sep" aria-hidden="true">·</span><span class="num">${escapeHtml(p)}</span>`).join(''));
}

// ---------- the card ----------
let card = null;
const vm = { verdict: CONNECTING, banner: null, path: null, busyLegend: false };
function push(change) {
  Object.assign(vm, change);
  if (card) card.update({ ...vm });
}

function onDiagnosis(diagnosis) {
  const verdict = presentVerdict(diagnosis);
  frame(verdict.level);
  updateHeader(diagnosis, verdict.level);
  push({ verdict });
}

function onTargets(targets) {
  const raw = pathNodes(targets);
  pillMetrics(targets, raw);
  push({ path: presentPath(targets, raw) });
}

// ---------- the notices over the chain: first run, route changed ----------
// A notice: { level, icon, title, text, progress (the text is the discovery's progress), actions: [{action, arg, label,
// primary, needsEdit}] }; null for none.
const review = (arg, title, text) => ({
  level: 'unknown', icon: 'path', title, text, progress: false,
  actions: [{ action: 'openPathEditor', arg, label: 'Review', primary: true, needsEdit: true }]
});
let notice = ''; // which notice shows: 'first' (the first run's) or 'drift'

// First run (a path never applied: revision 0, the default targets are probed): discovery starts by itself (the server
// starts it at most once per process; later loads show that run) and the notice offers its draft for review in the path
// editor (dialogs/path-editor). The automatic run looks up no operators (the ASN lookup sends hop addresses to a public
// service): the editor's Rediscover, with its disclosure, can. Until Apply the defaults keep probing, so the dashboard is
// never empty. True when it is a first run.
export async function firstRun() {
  let path;
  try { path = await fetchPath(); } catch (_) { return false; }
  hopDevices.clear();
  (path.hops || []).forEach((h) => { if (h.device_id && ['local', 'gateway'].includes(h.category)) hopDevices.set(h.id, h.device_id); });
  if (path.revision !== 0) { push({ banner: null }); return false; }
  firstRunBanner();
  return true;
}

async function firstRunBanner() {
  await util.accessKnown; // the sign-in state first
  if (!util.canEdit()) { push({ banner: null }); return; } // a viewer without the token: discovery and applying a path are the owner's
  notice = 'first';
  const finding = (text) => push({ banner: { level: 'unknown', icon: 'route', title: 'Finding your network path…', text, progress: true, actions: [] } });
  finding('Tracing the hops to the internet.');
  try {
    const id = await startDiscovery({ asnLookup: false, snmp: false, firstRun: true });
    const run = id ? await waitDiscovery(id, finding) : null;
    if (run && run.state === 'done') {
      const homeFound = ((run.draft && run.draft.hops) || []).some((h) => h.category === 'local' || h.category === 'gateway');
      push({ banner: homeFound
        ? review(`discovery:${id}`, 'Pengumon found your network path', '· Review it: until you apply it, the default targets are probed.')
        : review(`discovery:${id}`, 'Pengumon could not find your router', '· Only public servers answered. Review the draft: until you apply a path, the default targets are probed.') });
      return;
    }
  } catch (_) { /* no discovery in this build, or it failed: the defaults can be reviewed all the same */ }
  push({ banner: review('', 'Review your network path', '· The default targets are probed until you apply a path of your own.') });
}

// The route changed (GET /api/path/drift: discovery reruns quietly, daily and after an incident): "Your path changed ·
// Review" opens the editor with that discovery's draft; Dismiss marks these changes seen. Checked at the start and
// every few minutes; a build without the check shows nothing.
const DRIFT_EVERY_MS = 5 * 60 * 1000;
let driftKey = '';

export function driftBanner(drift, edit) {
  const changes = drift.changes;
  const first = String(changes[0].text || '').replace(/\.$/, '');
  const more = changes.length > 1 ? ` (and ${changes.length - 1} more)` : '';
  const goodAt = drift.good_at ? util.localFormat(drift.good_at * 1000, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
  const failed = drift.state === 'failed' ? ` The latest route check could not run, so this is from the check on ${goodAt || 'an earlier day'}.` : '';
  const actions = [{ action: 'openPathEditor', arg: `discovery:${drift.discovery_id}`, label: 'Review', primary: true, needsEdit: true }];
  if (edit) actions.push({ action: 'dismissPathDrift', label: 'Dismiss', needsEdit: true });
  return { level: 'warn', icon: 'route', title: 'Your path changed', text: `· ${first}${more}.${failed}`, progress: false, actions };
}

export async function driftNotice() {
  let drift;
  try { drift = await fetchDrift(); } catch (_) { return; }
  // a failed check keeps the changes of the last good one until they are reviewed
  const changes = drift && (drift.state === 'changed' || drift.state === 'failed') && drift.discovery_id ? (drift.changes || []) : [];
  if (!changes.length) {
    if (notice === 'drift') { notice = ''; push({ banner: null }); }
    return;
  }
  const key = `${drift.discovery_id}|${drift.checked_at}|${drift.state}`;
  if (key === driftKey && notice === 'drift') return;
  driftKey = key;
  notice = 'drift';
  push({ banner: driftBanner({ ...drift, changes }, util.canEdit()) });
}

async function dismiss(el) {
  el.disabled = true;
  try {
    await dismissDrift();
    notice = '';
    push({ banner: null });
  } catch (_) {
    el.disabled = false;
  }
}

registerActions('click', { dismissPathDrift: (el) => dismiss(el) });
// the route-change notice can name a time (the last good check): on the page's clock
document.addEventListener('zonechange', () => { if (notice === 'drift') { driftKey = ''; driftNotice(); } });

// ---------- the fixture (the parity test of the card's views, tests/ui/steps/core.mjs) ----------
export function fixture() {
  const t = (id, name, category, address, ms, extra = {}) => ({
    target_id: id, name, category, address, enabled: true, status: 'up', recent_latencies: Array(30).fill(ms), last_ms: ms,
    loss_pct: 0, loss_recent_pct: 0, jitter_ms: 0.4, group: '', ...extra
  });
  const targets = [
    t('fx_router', 'Example router', 'local', '192.168.88.1', 1.2),
    t('fx_modem', 'Example modem', 'gateway', '192.168.88.254', 4.5, { status: 'degraded', loss_recent_pct: 20, recent_latencies: [...Array(28).fill(4.5), null, null] }),
    t('fx_public', 'Example resolver', 'public', '192.0.2.53', 14.8),
    t('fx_nas', 'Example NAS', 'custom', '198.51.100.7', 0.6)
  ];
  const verdict = presentVerdict({ status: 'degraded', title: 'Loss at the gateway', description: 'Example modem lost 20% of recent probes.', active_incident: { id: 7 } });
  const banner = driftBanner({ discovery_id: 'fx', state: 'changed', changes: [{ text: 'Hop 2 is new.' }, { text: 'Hop 3 left.' }] }, true);
  return { verdict, banner, path: presentPath(targets), busyLegend: true };
}

// ---------- mounting ----------
export function mountDiagnosis(bus, ctx) {
  const article = document.getElementById('diagnosisBanner');
  card = createCard({ panel: 'diagnosis', root: article.querySelector('.panel-b'), standard, fixture });
  push({ busyLegend: !!(ctx && ctx.featureAvailable('line_load')) });
  bus.subscribe('targets', onTargets);
  firstRun().then((first) => {
    if (first) return;
    driftNotice();
    setInterval(driftNotice, DRIFT_EVERY_MS);
  });
  return bus.subscribe('diagnosis', onDiagnosis);
}
