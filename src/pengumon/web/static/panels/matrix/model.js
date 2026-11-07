// Hop details, the model: one row per target (loss, latency range, last, jitter, totals, the last 60 probes), from the
// `targets` topic. Every figure but "Lost since start" covers the same window, the last 60 probes; each row is named the
// way the path health chain names it (Router, Gateway, ISP hop N, Internet <address>, DNS lookup N) with the configured
// name under it, and the address only when that name does not already say it. present() makes the view model every view
// draws (view.js is the standard one; docs/themes.md, "Drawing a card"); live data comes every second, so a view patches
// its rows.

import { bus } from '../../js/bus.js';
import { createCard } from '../../js/cards.js';
import { latestMs, STATUS, targetList } from '../../js/util.js';
import { quietTargets, shortLabels } from '../../js/path.js';
import standard from './view.js';

let card = null;

// The loss chip's colour: red when the server calls the target down, amber for any recent loss, else plain. The limits
// that make a target down or degraded are the server's (help hop.loss quotes them).
const lossLevel = (t, pct) => (t.status === 'down' ? 'crit' : pct > 0 ? 'warn' : 'good');

// Whether the configured name already says the address: a DNS lookup names its domain ("google.com @system"), many hops
// carry their IP in the name.
const nameSaysAddress = (name, address) => !address || (name || '').includes(address.split(/[\s@]/)[0]);

const has = (v) => v !== null && v !== undefined;
const r2 = (v) => (has(v) ? +Number(v).toFixed(2) : v); // the server sends raw floats

// The view model: { rows: [row...] } (a row: id, label, name, addr, sayAddr, labelTitle, subTitle, loss {level, text,
// icon, title, chip}, range {text, aria, from, to, mean}, last {text, tone}, jitter, total, spark {id, values, label}).
// `labels`: target id -> the short name; `quiet`: path.js quietTargets.
export function present(targets, labels, quiet) {
  // min and max over the same last 60 probes as the average and the jitter (the server's min_ms / max_ms are since
  // start, which made one old spike look like a current problem)
  const windowStats = (t) => {
    const ok = (t.recent_latencies || []).filter(has);
    return ok.length ? { min: Math.min(...ok), max: Math.max(...ok), avg: t.avg_ms } : { min: null, max: null, avg: null };
  };
  const stats = new Map(targets.map((t) => [t.target_id, windowStats(t)]));
  // one scale for every row's range bar, so the rows compare
  const scale = Math.max(20, ...[...stats.values()].map((w) => (has(w.max) ? w.max : 0)));
  const pct = (v) => Math.min(100, (v / scale) * 100);
  return {
    rows: targets.map((t) => {
      // Recent 1-minute rolling loss
      const recentLoss = has(t.loss_recent_pct) ? t.loss_recent_pct : t.loss_pct;
      const ignores = quiet.has(t.target_id);
      const unprobed = ignores && quiet.get(t.target_id).kind !== 'ping';
      const level = ignores ? 'unknown' : lossLevel(t, recentLoss);
      const w = stats.get(t.target_id);
      const totalLost = Math.max(0, t.sent - t.received);
      const ms = (v) => (has(v) ? `${r2(v)} ms` : '-');
      const latest = latestMs(t); // not last_ms: that keeps the last answer through an outage
      const lost = t.sent > 0 && !has(latest); // before the first probe there is nothing to call lost
      const sayAddr = !nameSaysAddress(t.name, t.address);
      let range;
      if (has(w.min) && has(w.max)) {
        range = { text: `${r2(w.min)} · ${has(w.avg) ? r2(w.avg) : '-'} · ${r2(w.max)} ms`,
          aria: `last 60 probes: min ${r2(w.min)}, average ${has(w.avg) ? r2(w.avg) : 'unknown'}, max ${r2(w.max)} ms`,
          from: pct(w.min), to: pct(w.max), mean: has(w.avg) ? pct(w.avg) : null };
      } else {
        range = { text: unprobed ? 'not probed' : 'no reply', aria: null, from: 0, to: 0, mean: null };
      }
      let last;
      if (unprobed) last = { text: 'not probed', tone: 'ink-muted' };
      else last = { text: lost ? (ignores ? 'no reply' : 'LOST') : ms(latest), tone: lost ? (ignores ? 'ink-muted' : 'ink-crit') : '' };
      // loss_pct is rounded to 0.1 %: a few lost probes among many read 0.0, so the count decides
      const totalPct = totalLost > 0 && t.loss_pct < 0.1 ? '<0.1' : t.loss_pct;
      return {
        id: t.target_id,
        label: labels[t.target_id] || t.name,
        labelTitle: t.name,
        name: t.name,
        addr: t.address,
        sayAddr,
        subTitle: sayAddr ? `${t.name} · ${t.address}` : t.name,
        loss: {
          level,
          chip: recentLoss > 0 || level !== 'good', // a plain loss figure is not a coloured chip
          icon: STATUS[level].icon,
          text: unprobed ? 'no IPv6' : ignores ? 'no ping' : `${recentLoss}%`,
          title: unprobed ? quiet.get(t.target_id).why
            : ignores ? 'Does not answer ping, but the hops after it do: traffic passes through it (many provider routers ignore ping)' : ''
        },
        range,
        last,
        jitter: ms(t.jitter_ms),
        total: `${totalPct}% (${totalLost.toLocaleString()} / ${t.sent.toLocaleString()})`,
        spark: { id: t.target_id, values: t.recent_latencies || [], label: `Recent latency of ${t.name}` }
      };
    })
  };
}

// Made-up targets for the parity test of the card's views: a healthy one, one that ignores ping and one that lost its
// newest probe.
export function fixture() {
  const probes = (n, fn) => Array.from({ length: 60 }, (_, i) => fn(i, n));
  const targets = [
    { target_id: 'fx_router', name: 'Example router', address: '192.0.2.1', status: 'up', loss_pct: 0, loss_recent_pct: 0, avg_ms: 2.4, jitter_ms: 0.4, sent: 4000, received: 4000, last_ms: 2.1,
      recent_latencies: probes(0, (i) => 2 + (i % 4) * 0.3) },
    { target_id: 'fx_hop', name: 'Example hop 2', address: '198.51.100.2', status: 'down', loss_pct: 100, loss_recent_pct: 100, avg_ms: null, jitter_ms: null, sent: 4000, received: 0, last_ms: null,
      recent_latencies: probes(0, () => null) },
    { target_id: 'fx_dns', name: 'example.com @system', address: 'example.com', status: 'degraded', loss_pct: 0.05, loss_recent_pct: 1.7, avg_ms: 24.5, jitter_ms: 3.2, sent: 3000, received: 2998, last_ms: 20,
      recent_latencies: probes(0, (i) => (i === 59 ? null : 20 + (i % 7))) }
  ];
  return present(targets, { fx_router: 'Router', fx_hop: 'ISP hop 2', fx_dns: 'DNS lookup 1' },
    new Map([['fx_hop', { kind: 'ping', word: 'Ignores ping', why: 'Does not answer ping, but the hops after it do' }]]));
}

// Cost of the view model and its draw per message (sparklines included), for the messages the card drew (none while it
// is off screen): a rolling window, read by the smoke test through window.__pengumonPerf (a plain object, nothing is
// logged).
const matrixCost = {
  samples: [],
  count: 0, // every message drawn so far
  record(ms, rows) {
    this.samples.push(ms);
    if (this.samples.length > 120) this.samples.shift();
    window.__pengumonPerf = { matrixMs: this.samples.slice(), targets: rows, drawn: ++this.count };
  }
};

// Rows are built on the first message and updated in place after that.
export function mountMatrix(section) {
  // a message every second, the last card on the page: not drawn while it is off screen (js/cards.js)
  card = createCard({ panel: 'matrix', root: section.querySelector('.panel-b'), standard, fixture, offscreen: 'defer' });
  return bus.subscribe('targets', (targets) => {
    const t0 = performance.now();
    const list = targetList().length ? targetList() : targets.map((t) => ({ id: t.target_id, ...t }));
    const vm = present(targets, shortLabels(list), quietTargets(targets));
    if (card.update(vm)) matrixCost.record(performance.now() - t0, vm.rows.length);
  });
}
