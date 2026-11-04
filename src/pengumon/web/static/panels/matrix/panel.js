// Hop details: one row per target (loss, latency range, last, jitter, totals, the last 60 probes), from the `targets` topic.
// Every figure but "Lost since start" covers the same window, the last 60 probes; each row is named the way the path
// health chain names it (Router, Gateway, ISP hop N, Internet <address>, DNS lookup N) with the configured name under it,
// and the address only when that name does not already say it. The columns have fixed widths (panel.css), so a figure
// that changes width (LOST, a large jitter) does not move the others.

import { getTargetInfo, icon, latestMs, setClass, setText, STATUS, targetList } from '../../js/util.js';
import { quietTargets, shortLabels } from '../../js/path.js';
import { createSparkline, drawSparkline } from '../../js/sparkline.js';
import './help.js';

// Rows are created once per target and updated in place every tick, so the page does not re-layout (and a hover or
// selection survives) once a second. On a narrow panel the table's rows become cards (components.css .dlist).
const hopRefs = new Map();

let hopLayoutKey = '';

// The loss chip's colour: red when the server calls the target down, amber for any recent loss, else plain. The limits
// that make a target down or degraded are the server's (help hop.loss quotes them).
const lossLevel = (t, pct) => (t.status === 'down' ? 'crit' : pct > 0 ? 'warn' : 'good');

// Whether the configured name already says the address: a DNS lookup names its domain ("google.com @system"), many hops
// carry their IP in the name.
export const nameSaysAddress = (name, address) => !address || (name || '').includes(address.split(/[\s@]/)[0]);

function buildHopRow(t, tbody) {
  const refs = {};
  const tr = document.createElement('tr');
  tr.className = 'row-click';
  tr.innerHTML = `
    <td class="lead"><span class="primary" data-f="label"></span><span class="sub" data-f="sub"><span data-f="name"></span><span data-f="addrWrap"> · <span class="mono" data-f="addr"></span></span></span></td>
    <td class="aside"><span class="chip" data-f="loss"></span></td>
    <td class="wide"><span class="range" role="img" data-f="range"><i data-f="band"></i><b data-f="mark"></b></span><span class="range-n num" data-f="rangeText"></span></td>
    <td class="r num" data-l="Last"><b data-f="last"></b></td>
    <td class="r num hide-narrow" data-f="jitter"></td>
    <td class="r num hide-narrow c-total" data-f="total" title="Every probe since pengumon started"></td>
    <td class="hide-narrow spark-td c-spark" data-f="sparkCell"></td>`;
  tr.dataset.action = 'openTargetHistory';
  tr.dataset.arg = t.target_id;
  tr.title = 'Show the latency and loss history';
  tr.querySelectorAll('[data-f]').forEach((el) => { refs[el.dataset.f] = el; });
  refs.spark = createSparkline(`Recent latency of ${t.name}`);
  refs.sparkCell.appendChild(refs.spark);
  tbody.appendChild(tr);
  return refs;
}

function updateHopMatrix(targets) {
  const tbody = document.getElementById('hopMatrixBody');

  // Only a change in the set or order of targets rebuilds the structure.
  const layoutKey = targets.map(t => t.target_id).join('|');
  if (layoutKey !== hopLayoutKey) {
    hopLayoutKey = layoutKey;
    hopRefs.clear();
    tbody.innerHTML = '';
    targets.forEach(t => hopRefs.set(t.target_id, buildHopRow(t, tbody)));
  }
  const has = (v) => v !== null && v !== undefined;
  // min and max over the same last 60 probes as the average and the jitter (the server's min_ms / max_ms are since
  // start, which made one old spike look like a current problem)
  const windowStats = (t) => {
    const ok = (t.recent_latencies || []).filter(has);
    return ok.length ? { min: Math.min(...ok), max: Math.max(...ok), avg: t.avg_ms } : { min: null, max: null, avg: null };
  };
  const stats = new Map(targets.map((t) => [t.target_id, windowStats(t)]));
  // no reply, but later hops answer: traffic passes; or an IPv6 target this host cannot probe (path.js)
  const quiet = quietTargets(targets);
  const labels = shortLabels(targetList().length ? targetList() : targets.map((t) => ({ id: t.target_id, ...t })));
  // one scale for every row's range bar, so the rows compare
  const scale = Math.max(20, ...[...stats.values()].map((w) => (has(w.max) ? w.max : 0)));
  const pct = (v) => `${Math.min(100, (v / scale) * 100).toFixed(1)}%`;

  targets.forEach((t) => {
    const r = hopRefs.get(t.target_id);
    if (!r) return;
    // Recent 1-minute rolling loss
    const recentLoss = (t.loss_recent_pct !== undefined) ? t.loss_recent_pct : t.loss_pct;
    const ignores = quiet.has(t.target_id);
    const unprobed = ignores && quiet.get(t.target_id).kind !== 'ping';
    const level = ignores ? 'unknown' : lossLevel(t, recentLoss);
    const w = stats.get(t.target_id);
    const totalLost = Math.max(0, t.sent - t.received);
    const r2 = (v) => (has(v) ? +Number(v).toFixed(2) : v); // the server sends raw floats
    const ms = (v) => (has(v) ? `${r2(v)} ms` : '-');
    const latest = latestMs(t); // not last_ms: that keeps the last answer through an outage
    const lost = t.sent > 0 && !has(latest); // before the first probe there is nothing to call lost

    setText(r.label, labels[t.target_id] || t.name);
    setText(r.name, t.name);
    setText(r.addr, t.address);
    r.addrWrap.hidden = nameSaysAddress(t.name, t.address);
    r.label.title = t.name; r.sub.title = r.addrWrap.hidden ? t.name : `${t.name} · ${t.address}`;
    if (r.loss.dataset.key !== `${level}|${recentLoss}|${ignores}|${unprobed}`) {
      r.loss.dataset.key = `${level}|${recentLoss}|${ignores}|${unprobed}`;
      r.loss.className = recentLoss > 0 || level !== 'good' ? `chip st st-${level}` : 'chip';
      r.loss.innerHTML = unprobed ? `${icon(STATUS.unknown.icon, 'sm')}no IPv6`
        : ignores ? `${icon(STATUS.unknown.icon, 'sm')}no ping` : `${icon(STATUS[level].icon, 'sm')}${recentLoss}%`;
      r.loss.title = unprobed ? quiet.get(t.target_id).why
        : ignores ? 'Does not answer ping, but the hops after it do: traffic passes through it (many provider routers ignore ping)' : '';
    }
    if (has(w.min) && has(w.max)) {
      r.band.style.left = pct(w.min);
      r.band.style.width = `calc(${pct(w.max)} - ${pct(w.min)})`;
      r.mark.hidden = !has(w.avg);
      if (has(w.avg)) r.mark.style.left = pct(w.avg);
      setText(r.rangeText, `${r2(w.min)} · ${has(w.avg) ? r2(w.avg) : '-'} · ${r2(w.max)} ms`);
      r.range.setAttribute('aria-label', `last 60 probes: min ${r2(w.min)}, average ${has(w.avg) ? r2(w.avg) : 'unknown'}, max ${r2(w.max)} ms`);
    } else {
      r.band.style.width = '0';
      r.mark.hidden = true;
      setText(r.rangeText, unprobed ? 'not probed' : 'no reply');
    }
    if (unprobed) { setText(r.last, 'not probed'); setClass(r.last, 'ink-muted'); }
    else { setText(r.last, lost ? (ignores ? 'no reply' : 'LOST') : ms(latest)); setClass(r.last, lost ? (ignores ? 'ink-muted' : 'ink-crit') : ''); }
    setText(r.jitter, ms(t.jitter_ms));
    // loss_pct is rounded to 0.1 %: a few lost probes among many read 0.0, so the count decides
    const totalPct = totalLost > 0 && t.loss_pct < 0.1 ? '<0.1' : t.loss_pct;
    setText(r.total, `${totalPct}% (${totalLost.toLocaleString()} / ${t.sent.toLocaleString()})`);

    drawSparkline(r.spark, t.recent_latencies || [], getTargetInfo(t.target_id).color);
  });
}

// Cost of updateHopMatrix per message (sparklines included): a rolling window, read by the smoke test through
// window.__pengumonPerf (a plain object, nothing is logged).
const matrixCost = {
  samples: [],
  record(ms) {
    this.samples.push(ms);
    if (this.samples.length > 120) this.samples.shift();
    window.__pengumonPerf = { matrixMs: this.samples.slice(), targets: hopRefs.size };
  }
};

// Rows are built on the first message and updated in place after that.
export function mountMatrix(bus) {
  return bus.subscribe('targets', (targets) => {
    const t0 = performance.now();
    updateHopMatrix(targets);
    matrixCost.record(performance.now() - t0);
  });
}

export default {
  id: 'matrix',
  title: 'Hop details',
  tab: 'live',
  icon: 'route',
  core: true,
  available: () => true,
  mount: (section, ctx) => mountMatrix(ctx.bus)
};
