// Path health: the classifier's verdict for the whole line (the `diagnosis` topic) and the path from this host to the
// internet, hop by hop (the `targets` topic), shown as the hero panel and as the header's status pill. Until the first
// message the page says "Connecting" in grey and the hops are skeletons, never a green "healthy".
//
// The chain follows the targets' categories in path order: this host, the local network (router or mesh), the gateway
// (modem / line), every ISP hop, then the internet (the public targets as one node; DNS lookups stand in when there
// is no public target). A category with several targets shows its worst one and the count. Each node: the median of
// the last probes (big number), loss and jitter, and the last 60 probes as a sparkline; each link: how much latency
// that segment adds. A node opens that target's history; the Internet node and a folded run of ISP hops open the history
// of all their targets together (a switcher there shows one of them).

import { escapeHtml, icon, latestMs, setBadge, setText, STATUS } from '../../js/util.js';
import { sparkSvg } from '../../js/sparkline.js';
import { IGNORES_PING, IGNORES_PING_WHY, pathNodes } from '../../js/path.js';
import './help.js';

// The classifier's status -> the design's status level and its word.
const LEVEL = { healthy: 'good', degraded: 'warn', down: 'crit', busy: 'busy' };
const WORD = { healthy: 'Healthy', degraded: 'Degraded', down: 'Down', busy: 'Busy' };

export const levelOf = (status) => LEVEL[status] || 'unknown';

function verdictText(diagnosis) {
  if (diagnosis.status !== 'busy') return diagnosis.description;
  const load = diagnosis.load_pct !== null && diagnosis.load_pct !== undefined ? ` (${Math.round(diagnosis.load_pct)}% of capacity)` : '';
  return `Line busy - latency is expected under heavy load${load}`;
}

// The header: status pill, page tint and the tab title follow the verdict.
function updateHeader(diagnosis, level) {
  const pill = document.getElementById('statusPill');
  if (pill) pill.className = `status-pill st-${level}`;
  setText(document.getElementById('statusPillText'), diagnosis.title);
  setText(document.getElementById('statusPillWord'), WORD[diagnosis.status] || diagnosis.title); // the phone's pill: one word
  document.getElementById('topbar').className = `topbar st-${level}`;
  document.body.dataset.status = level;
  document.title = level === 'good' ? 'pengumon' : `${WORD[diagnosis.status]} · pengumon`;
}

export function updateDiagnosisBanner(diagnosis) {
  const level = levelOf(diagnosis.status);
  const card = document.getElementById('diagnosisBanner');
  card.dataset.status = level;
  card.classList.remove('st-good', 'st-warn', 'st-crit', 'st-busy', 'st-unknown');
  card.classList.add(`st-${level}`);
  setBadge(document.getElementById('statusBadge'), level, WORD[diagnosis.status] || diagnosis.status);
  setText(document.getElementById('diagnosisTitle'), diagnosis.title);
  setText(document.getElementById('diagnosisDesc'), verdictText(diagnosis));
  updateHeader(diagnosis, level);
}

// ---------- the chain (its nodes: path.js pathNodes) ----------
const STATE = { up: 'good', degraded: 'warn', down: 'crit' };
const has = (v) => v !== null && v !== undefined;

const fmtMs = (v) => (!has(v) ? '–' : v >= 100 ? String(Math.round(v)) : v.toFixed(1));
const fmtPct = (v) => (v >= 100 ? '100%' : `${(+v).toFixed(1)}%`);

// The origin of the chain: a small marker, not a hop (no status, no value). Hop statuses are on the nodes and links.
function hostNode(count) {
  return `<li class="origin" aria-label="This host: probing ${count} targets every second"><span class="origin-ic">${icon('host')}</span>
    <b class="origin-t">This host</b><span class="origin-m"><span data-f="hostCount">${count}</span> targets</span><span class="origin-m">every 1 s</span></li>`;
}

function nodeShell(n) {
  return `<li class="link" data-link="${escapeHtml(n.key)}" aria-hidden="true"><svg class="wire" viewBox="0 0 40 10" preserveAspectRatio="none"><line x1="0" y1="5" x2="40" y2="5"/></svg><span class="cut" hidden>${icon('close', 'sm')}</span><span class="delta"></span></li>
    <li class="node-wrap"><button type="button" class="node" data-node="${escapeHtml(n.key)}" data-action="openTargetHistory" data-range="live" data-arg="${escapeHtml(n.target)}"${n.history ? ` data-group="${escapeHtml(JSON.stringify(n.history))}"` : ''}>
      <span class="node-top"><span class="node-ic">${icon(n.icon)}</span><span class="node-id"><span class="node-kind"></span></span><span class="node-st"></span></span><span class="node-name"></span>
      <div class="node-val"></div><div class="node-meta"></div><span class="node-spark"></span></button></li>`;
}

function skeleton() {
  const node = '<li class="link" aria-hidden="true"></li><li class="node-wrap"><div class="node st-unknown"><span class="node-top"><span class="node-ic"></span><span class="skel" style="width:80px;height:12px"></span></span><div class="node-val"><span class="skel" style="width:58px;height:26px"></span></div><div class="node-meta"><span class="skel" style="width:90px;height:10px"></span></div><div class="skel" style="height:26px"></div></div></li>';
  return `<li class="origin"><span class="origin-ic">${icon('host')}</span><b class="origin-t">This host</b><span class="origin-m">connecting…</span></li>${node.repeat(3)}`;
}

let chainKey = null; // null until the first message replaces the skeleton

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

function paintNode(el, link, n, prev) {
  const word = n.quiet ? IGNORES_PING : STATUS[n.level].word;
  // what a click opens follows the node (its worst target, or the members of a group can change under the same key)
  if (el.dataset.arg !== n.target) el.dataset.arg = n.target;
  const group = n.history ? JSON.stringify(n.history) : '';
  if ((el.dataset.group || '') !== group) {
    if (group) el.dataset.group = group;
    else delete el.dataset.group;
  }
  el.className = `node st-${n.level}${n.level !== 'good' && !n.quiet ? ' alert' : ''}`;
  el.setAttribute('aria-label', `${n.label}: ${n.name}, ${word}${has(n.ms) ? `, ${n.ms.toFixed(1)} milliseconds` : ''}. Show its history`);
  setText(el.querySelector('.node-kind'), n.count > 1 ? `${n.label} · ${n.count}` : n.label);
  // the configured name is long ("Hop 3: <provider> ..."): one line under the short label, whole in the tooltip
  const name = el.querySelector('.node-name');
  setText(name, n.name);
  name.title = n.title || n.name;
  const st = el.querySelector('.node-st');
  if (st.dataset.level !== n.level || st.title !== word) {
    st.dataset.level = n.level;
    st.title = word;
    st.innerHTML = icon(STATUS[n.level].icon);
  }
  el.querySelector('.node-val').innerHTML = has(n.ms) ? `<b class="num">${fmtMs(n.ms)}</b><span>ms</span>` : '<b class="node-word">No reply</b>';
  el.querySelector('.node-meta').innerHTML = n.quiet ? `<span title="${escapeHtml(IGNORES_PING_WHY)}">ignores ping · traffic passes</span>`
    : `<span class="${n.loss > 0 ? 'bad' : ''}">loss <b class="num">${fmtPct(n.loss)}</b></span>${has(n.jitter) && has(n.ms) ? `<span>jitter <b class="num">${(+n.jitter).toFixed(1)}</b></span>` : ''}`;
  el.querySelector('.node-spark').innerHTML = sparkSvg(n.spark, n.level);
  // the link before the node: what this segment adds (from the last hop before it that answers)
  const broken = !has(n.ms) && !n.quiet;
  const linkLevel = n.level;
  link.className = `link st-${linkLevel}${broken ? ' broken' : ''}`;
  link.querySelector('.cut').hidden = !broken;
  const delta = link.querySelector('.delta');
  delta.title = 'Latency this segment adds';
  let text = '';
  if (n.quiet) { text = '–'; delta.title = IGNORES_PING_WHY; }
  else if (broken) { text = 'no reply'; delta.title = 'No answer from this hop'; }
  else if (!prev || !has(prev.ms)) text = `${n.ms.toFixed(1)} ms`;
  else {
    ({ text, title: delta.title } = deltaText(n.ms - prev.ms));
  }
  setText(delta, text);
}

// How many nodes fit side by side before a label like "ISP hop 3" or a value like "23.2 ms" no longer does (the
// chain's dense styles: links of 34 px, 24 px in a medium hero); a vertical chain (narrow hero) takes any number.
const MIN_NODE = 112;
function fits(chain) {
  if (getComputedStyle(chain).flexDirection === 'column') return Infinity;
  const width = chain.clientWidth;
  return Math.floor((width - 74) / (MIN_NODE + (width < 1060 ? 24 : 34)));
}

// The targets that are no stage of the path (category custom: a NAS, a VPN, a server): one chip each under the chain.
function paintExtras(live) {
  const box = document.getElementById('pathExtra');
  if (!box) return;
  const extra = live.filter((t) => t.category === 'custom');
  box.hidden = !extra.length;
  const html = extra.map((t) => {
    const level = STATE[t.status] || 'unknown';
    return `<button type="button" class="pe st-${level}" data-action="openTargetHistory" data-arg="${escapeHtml(t.target_id)}" title="${escapeHtml(`${t.name} · ${t.address} · ${STATUS[level].word}. Show its history`)}">`
      + `${icon(STATUS[level].icon, 'sm')}<span class="pe-n">${escapeHtml(t.name)}</span><b class="num">${has(latestMs(t)) ? `${fmtMs(latestMs(t))} ms` : 'no reply'}</b></button>`;
  }).join('');
  const list = box.querySelector('.pe-list');
  if (list.dataset.html !== html) { list.dataset.html = html; list.innerHTML = html; }
}

let lastTargets = null;

function updateChain(targets) {
  lastTargets = targets;
  const chain = document.getElementById('pathChain');
  let nodes = pathNodes(targets);
  if (nodes.length > fits(chain)) nodes = pathNodes(targets, { collapseIsp: true });
  const key = nodes.map((n) => n.key).join('|');
  if (key !== chainKey) {
    chainKey = key;
    chain.innerHTML = hostNode(targets.length) + (nodes.length ? nodes.map(nodeShell).join('')
      : '<li class="chain-none">No router, modem, provider hop or public server is a target, so there is no path to draw. Add one as a target to see it here.</li>');
  }
  // more than five nodes, or four in a medium hero: narrower links and padding, no node icons in a medium hero
  chain.dataset.dense = nodes.length + 1 > 5 || (nodes.length >= 4 && chain.clientWidth < 1060) ? '1' : '0';
  chain.classList.add('is-live');
  setText(chain.querySelector('[data-f=hostCount]'), String(targets.length));
  let heard = null; // the last node before this one that answers
  nodes.forEach((n) => {
    const el = chain.querySelector(`[data-node="${CSS.escape(n.key)}"]`);
    const link = chain.querySelector(`[data-link="${CSS.escape(n.key)}"]`);
    if (el && link) paintNode(el, link, n, heard);
    if (has(n.ms)) heard = n;
  });
  paintExtras(targets.filter((t) => t.enabled !== false));
  // the header pill: the worst loss on the path (hops that only ignore ping left out) and the internet's latency
  const internet = nodes[nodes.length - 1];
  const worst = nodes.filter((n) => n.loss !== null).reduce((a, b) => (b.loss > (a ? a.loss : -1) ? b : a), null);
  const parts = [];
  if (worst && worst.loss > 0) parts.push(`${fmtPct(worst.loss)} loss`);
  if (internet && has(internet.ms)) parts.push(`${fmtMs(internet.ms)} ms to internet`);
  const metrics = document.getElementById('statusPillMetrics');
  if (metrics) metrics.innerHTML = parts.map((p) => `<span class="sep" aria-hidden="true">·</span><span class="num">${escapeHtml(p)}</span>`).join('');
}

// The verdict's Inspect button: shown while an incident is open.
function inspectButton(diagnosis) {
  const verdict = document.querySelector('#diagnosisBanner .verdict');
  let button = verdict.querySelector('.verdict-act');
  const id = diagnosis.active_incident && diagnosis.active_incident.id;
  if (!id) { if (button) button.remove(); return; }
  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn outline sm verdict-act';
    button.dataset.action = 'inspectIncident';
    button.innerHTML = `${icon('inspect')}Inspect`;
    verdict.appendChild(button);
  }
  button.dataset.arg = String(id);
}

export function mountDiagnosis(bus, ctx) {
  document.getElementById('pathChain').innerHTML = skeleton();
  const foot = document.getElementById('pathFoot');
  if (foot && ctx && ctx.featureAvailable('line_load')) {
    const busy = document.createElement('span');
    busy.innerHTML = `${icon('busy', 'sm ink-busy')}Busy (expected)`;
    foot.children[2].after(busy);
  }
  bus.subscribe('targets', updateChain);
  // a wider or narrower hero may fit a node per ISP hop, or no longer
  new ResizeObserver(() => { if (lastTargets) updateChain(lastTargets); }).observe(document.getElementById('pathChain'));
  return bus.subscribe('diagnosis', (d) => { updateDiagnosisBanner(d); inspectButton(d); });
}

export default {
  id: 'diagnosis',
  title: 'Path health',
  tab: 'live',
  icon: 'path',
  core: true,
  available: () => true,
  mount: (section, ctx) => mountDiagnosis(ctx.bus, ctx)
};
