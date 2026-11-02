// The probe path in the latency chart's terms: three series in path order, each from the targets of its categories.
// Router: the worst local target; line: the worst gateway target; internet: the median of the public targets (DNS
// lookups stand in when there is none). ISP hops and DNS stay out of the path series (at most three lines cross, as the
// design asks); the chart's "Each target" view, hop details and target history show every target.

import { latestMs } from './util.js';

export const SERIES = [
  { id: 'path_router', name: 'Router', cats: ['local'], color: '--s1', combine: 'worst' },
  { id: 'path_line', name: 'Line', cats: ['gateway'], color: '--s3', combine: 'worst' },
  { id: 'path_internet', name: 'Internet', cats: ['public'], fallback: ['dns'], color: '--s2', combine: 'median' }
];

const has = (v) => v !== null && v !== undefined;

export function median(values) {
  const v = values.filter(has).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// Which target ids feed which series: { seriesId: [targetId...] }, from [{id, category}] (the configured targets;
// disabled ones are left out).
export function seriesMembers(targets) {
  const out = {};
  const live = targets.filter((t) => t.enabled !== false);
  SERIES.forEach((s) => {
    let ids = live.filter((t) => s.cats.includes(t.category)).map((t) => t.id);
    if (!ids.length && s.fallback) ids = live.filter((t) => s.fallback.includes(t.category)).map((t) => t.id);
    if (ids.length) out[s.id] = ids;
  });
  return out;
}

// One value of a series from the values of its member targets (null for a lost probe).
export function combine(series, values) {
  if (!values.length) return null;
  if (series.combine === 'median') return median(values);
  const answered = values.filter(has);
  return answered.length === values.length ? Math.max(...answered) : null; // worst: a loss shows as a gap
}

// The short name every panel gives a target, in the path health chain's words: Router, Line, ISP hop N, Internet (with
// the address when there are several public servers, the Internet figure being their median), DNS lookup N; a custom target keeps its
// configured name. A number is added only when a category has several targets. The configured name stays the
// secondary text. From [{id, category, name, address, enabled}] in path order: { targetId: label }.
const SHORT = { local: 'Router', gateway: 'Line', isp: 'ISP hop', dns: 'DNS lookup' };

export function shortLabels(targets) {
  const live = targets.filter((t) => t.enabled !== false);
  const out = {};
  const byCat = {};
  live.forEach((t) => { (byCat[t.category] ||= []).push(t); });
  live.forEach((t) => {
    const group = byCat[t.category];
    const n = group.indexOf(t) + 1;
    const sameAddress = group.filter((o) => o.address === t.address).length > 1;
    if (t.category === 'public' && group.length === 1) out[t.id] = 'Internet';
    else if (t.category === 'public') out[t.id] = t.address && !sameAddress ? `Internet ${t.address}` : `Internet ${n}`;
    else if (t.category === 'isp') out[t.id] = `ISP hop ${n}`;
    else if (SHORT[t.category]) out[t.id] = group.length > 1 ? `${SHORT[t.category]} ${n}` : SHORT[t.category];
    else out[t.id] = t.name || t.id;
  });
  return out;
}

// The members of a median/worst group, named for a legend or a node: their addresses when each has its own (short),
// else the short labels. `group` and `all` are target lists with {id, category, name, address, enabled}.
export function memberNames(group, all = group) {
  const addrs = group.map((t) => t.address);
  if (addrs.every(Boolean) && new Set(addrs).size === addrs.length) return addrs;
  const labels = shortLabels(all);
  return group.map((t) => labels[t.id] || t.name || t.id);
}

// The Internet figure of the live `targets` topic, one for the hero node, the header pill and everything else: the
// median across the public targets (the same as the latency chart's Internet line), from each target's own median of its
// last 60 probes. `spark` is the same median probe by probe, the newest probes lined up. Targets with no probes yet
// give null. `group` is a list of {recent_latencies, last_ms}.
export function internetFigure(group) {
  const recent = group.map((t) => (t.recent_latencies || []).slice(-60));
  const own = group.map((t, i) => (recent[i].length ? median(recent[i]) : (has(t.last_ms) ? t.last_ms : null)));
  const len = Math.max(0, ...recent.map((r) => r.length));
  const spark = [];
  for (let k = len; k >= 1; k--) spark.push(median(recent.map((r) => r[r.length - k])));
  return { ms: median(own), spark };
}

// Hops that give no reply although a later stage of the path answers: up, only ignoring (or rate-limiting) ping, as many
// provider routers do. Shown as "Ignores ping", not as down, and left out of the loss figures. The server decides it
// (engine/path_rules.py, one definition for the dashboard, the reports, the dossier and the classifier) and flags each
// target of the live topic with `ignores_ping`; this only reads the flag. `targets` are the live topic's
// ({target_id, ignores_ping, enabled}); returns their target ids.
export function ignoringPing(targets) {
  return new Set(targets.filter((t) => t.enabled !== false && t.ignores_ping === true).map((t) => t.target_id));
}

// How every panel says it (the path's node, Hop details, Uptime, target history, the report): the word, and why.
export const IGNORES_PING = 'Ignores ping';
export const IGNORES_PING_WHY = 'This hop does not answer ping, but the hops after it do, so traffic passes through it: many provider '
  + 'routers ignore or rate-limit ping. What this part of the path adds shows on the next hop that answers.';
// Where a strip of uptime blocks would be (Uptime, target history): it would only be red, and its loss counts nowhere.
export const IGNORES_PING_STRIP = `${IGNORES_PING} · traffic passes, not counted as an outage`;

// The set of a list's targets that ignore ping as a string, to tell whether it changed between two live messages.
export const quietKey = (quiet) => [...quiet].sort().join('|');

// ---------- the path's nodes (Path health's chain, and the figure a target history opened from a node shows) ----------
const STAGES = [
  { cats: ['local'], kind: 'Router', icon: 'router' },
  { cats: ['gateway'], kind: 'Line', icon: 'modem' },
  { cats: ['isp'], kind: 'ISP', icon: 'isp', each: true },
  { cats: ['public'], kind: 'Internet', icon: 'globe', fallback: ['dns'], redundant: true }
];
const RANK = { up: 0, degraded: 1, down: 2 };
const STATE = { up: 'good', degraded: 'warn', down: 'crit' };

const withId = (t) => ({ ...t, id: t.target_id }); // the live topic keys targets by target_id, path.js by id

// One node per stage (one per target for ISP hops), from the targets the live message lists. `collapseIsp`: the ISP hops
// as one node instead (a long path in a hero too narrow for a node each): the worst hop decides its status and the last
// hop that answers its latency, so the links on either side still add up; Hop details lists every hop.
export function pathNodes(targets, { collapseIsp = false } = {}) {
  const live = targets.filter((t) => t.enabled !== false);
  const quiet = ignoringPing(live);
  const nodes = [];
  STAGES.forEach((stage) => {
    let list = live.filter((t) => stage.cats.includes(t.category));
    if (!list.length && stage.fallback) list = live.filter((t) => stage.fallback.includes(t.category));
    if (!list.length) return;
    const each = stage.each && !collapseIsp;
    const groups = each ? list.map((t) => [t]) : [list];
    groups.forEach((group, gi) => {
      // a hop that only ignores ping neither makes its node look down nor adds its loss
      const heard = group.filter((t) => !quiet.has(t.target_id));
      const silent = !heard.length;
      const judged = silent ? group : heard;
      const worst = judged.reduce((a, b) => (RANK[b.status] > RANK[a.status] ? b : a));
      // redundant probes (public resolvers): down only when all are down, degraded when any is not up
      let status = worst.status;
      if (stage.redundant && group.length > 1) {
        const up = group.filter((t) => t.status !== 'down');
        status = !up.length ? 'down' : (group.some((t) => t.status !== 'up') ? 'degraded' : 'up');
      }
      const run = stage.each && collapseIsp && group.length > 1;
      // the Internet node is the median across its targets (as in the latency chart); a run of ISP hops shows its last
      // hop that answers; other nodes show their worst
      const rep = run ? ([...heard].reverse().find((t) => has(latestMs(t))) || worst) : worst;
      const fig = stage.redundant && group.length > 1 ? internetFigure(group) : null;
      const loss = silent ? null : Math.max(...heard.map((t) => (has(t.loss_recent_pct) ? t.loss_recent_pct : t.loss_pct)));
      const recent = fig ? fig.spark : (rep.recent_latencies || []);
      const ignored = group.length - heard.length;
      const runName = `${status === 'up' ? `${group.length} hops, ${ignored ? `${heard.length} answer` : 'all answer'}` : `worst: ${worst.name}`}${ignored ? ` · ${ignored} ignore${ignored > 1 ? '' : 's'} ping` : ''}`;
      // a node of several targets opens their history together (dialogs/target-history): the members with the names
      // this node gives them, and the rule of the node's figure (the median, or the last hop that answers)
      let history = null;
      if (fig || run) {
        const labels = fig ? null : shortLabels(live.map(withId));
        const names = fig ? memberNames(group.map(withId), live.map(withId)) : group.map((t) => labels[t.target_id] || t.name);
        const what = group[0].category === 'dns' ? 'DNS lookups' : 'public servers';
        history = {
          title: fig ? `${stage.kind} · median of ${group.length} ${what}` : `${stage.kind} · ${group.length} hops`,
          rule: fig ? 'median' : 'last', category: group[0].category,
          members: group.map((t, k) => ({ id: t.target_id, short: names[k], name: t.name }))
        };
      }
      nodes.push({
        key: `${stage.kind}:${each ? worst.target_id : run ? 'run' : ''}`, kind: stage.kind,
        label: each ? `${stage.kind} hop ${gi + 1}` : stage.kind, icon: stage.icon, count: group.length,
        name: fig ? memberNames(group.map(withId), live.map(withId)).join(', ') : run ? runName : worst.name,
        title: fig ? `Median of ${group.map((t) => t.name).join(', ')}` : run ? group.map((t) => t.name).join('\n') : worst.name,
        target: (run && status === 'up' ? rep : worst).target_id, history, level: silent ? 'unknown' : STATE[status] || 'unknown', status, quiet: silent,
        ms: fig ? fig.ms : (recent.length ? median(recent.slice(-60)) : (has(rep.last_ms) ? rep.last_ms : null)),
        loss, jitter: rep.jitter_ms, spark: recent.slice(-60)
      });
    });
  });
  return nodes;
}
