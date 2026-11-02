// The probe path in the latency chart's terms: three series in path order (four with IPv6 servers), each from the targets
// of its categories. Router: the worst local target; line: the worst gateway target; internet: the median of the public
// targets outside the IPv6 group (DNS lookups stand in when there is none); "Internet (IPv6)": the median of the IPv6
// group, as its own line like Path health's own node. ISP hops and DNS stay out of the path series (at most three lines cross, as the
// design asks); the chart's "Each target" view, hop details and target history show every target.

import { latestMs } from './util.js';

export const V6_GROUP = 'internet_v6';

export const SERIES = [
  { id: 'path_router', name: 'Router', cats: ['local'], color: '--s1', combine: 'worst' },
  { id: 'path_line', name: 'Line', cats: ['gateway'], color: '--s3', combine: 'worst' },
  { id: 'path_internet', name: 'Internet', cats: ['public'], fallback: ['dns'], color: '--s2', combine: 'median' },
  // the IPv6 servers discovery proposes as their own group: a line of their own, as Path health has a node of their own
  { id: 'path_internet_v6', name: 'Internet (IPv6)', cats: ['public'], group: V6_GROUP, color: '--s4', combine: 'median' }
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
  const v6 = live.filter((t) => t.category === 'public' && t.group === V6_GROUP);
  const v4 = live.filter((t) => t.category === 'public' && t.group !== V6_GROUP);
  SERIES.forEach((s) => {
    let ids;
    if (s.group) ids = v4.length ? v6.map((t) => t.id) : []; // Path health's rule: the first group is "Internet", the IPv6 one is its own node only beside it
    else if (s.cats.includes('public')) ids = (v4.length ? v4 : v6).map((t) => t.id);
    else ids = live.filter((t) => s.cats.includes(t.category)).map((t) => t.id);
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

// An IPv6 target while this host has no IPv6 (no route, or none in the OS): not probed, so no samples, no loss and no
// verdict. The server decides it (engine/prober.py, re-checked every few minutes) and flags the target of the live topic
// with `ipv6_unavailable`; it is shown the way a hop that ignores ping is, neutral, with its own words.
export const NO_IPV6 = 'IPv6 unavailable on this host';
export const NO_IPV6_WHY = 'This host has no IPv6 route right now, so this IPv6 target is not probed: no loss is recorded and it is not '
  + 'an outage. Pengumon checks again every few minutes and probes it as soon as IPv6 works.';
export const NO_IPV6_STRIP = `${NO_IPV6} · not probed, not counted`;

// The words for each kind of neutral target: the state's name, why, the text where its strip would be, and the short
// line under a path node.
const IGNORES = { kind: 'ping', word: IGNORES_PING, why: IGNORES_PING_WHY, strip: IGNORES_PING_STRIP, meta: 'ignores ping · traffic passes' };
const UNPROBED = { kind: 'ipv6', word: NO_IPV6, why: NO_IPV6_WHY, strip: NO_IPV6_STRIP, meta: 'not probed · no IPv6 here' };

// The targets shown neutral instead of up or down: hops that ignore ping, and IPv6 targets this host cannot probe now.
// A Map from target id to its words (above); `.has(id)` is what most panels ask. `targets` are the live topic's.
export function quietTargets(targets) {
  const out = new Map();
  ignoringPing(targets).forEach((id) => out.set(id, IGNORES));
  targets.forEach((t) => { if (t.enabled !== false && t.ipv6_unavailable === true) out.set(t.target_id, UNPROBED); });
  return out;
}

// The words of a target that is not in a quietTargets map (a hop that ignores ping, the older and commoner case).
export const quietWords = (quiet, id) => (quiet && quiet.get && quiet.get(id)) || IGNORES;

// The set (or quietTargets map) of a list's neutral targets as a string, to tell whether it changed between two live
// messages; the kind is part of it, so a target that turns from one kind to the other is drawn again.
export const quietKey = (quiet) => (quiet instanceof Map ? [...quiet].map(([id, w]) => `${id}:${w.kind}`) : [...quiet]).sort().join('|');

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

// Adjacent targets with the same group (the path editor's "one node with the hop before") as one list each; the
// others alone.
function byGroup(list) {
  const out = [];
  list.forEach((t, i) => {
    if (i > 0 && t.group && t.group === list[i - 1].group) out[out.length - 1].push(t);
    else out.push([t]);
  });
  return out;
}

// The public servers by group (a server without one counts as "internet", today's single Internet node), in the order
// each group first appears.
function byServerGroup(list) {
  const out = new Map();
  list.forEach((t) => {
    const g = t.group || 'internet';
    if (!out.has(g)) out.set(g, []);
    out.get(g).push(t);
  });
  return [...out.values()];
}

// One node per stage (one per target, or per group of targets, for ISP hops), from the targets the live message lists. `collapseIsp`: the ISP hops
// as one node instead (a long path in a hero too narrow for a node each): the worst hop decides its status and the last
// hop that answers its latency, so the links on either side still add up; Hop details lists every hop.
export function pathNodes(targets, { collapseIsp = false } = {}) {
  const live = targets.filter((t) => t.enabled !== false);
  const quiet = quietTargets(live);
  const nodes = [];
  STAGES.forEach((stage) => {
    let list = live.filter((t) => stage.cats.includes(t.category));
    if (!list.length && stage.fallback) list = live.filter((t) => stage.fallback.includes(t.category));
    if (!list.length) return;
    const each = stage.each && !collapseIsp;
    const groups = each ? byGroup(list) : stage.redundant ? byServerGroup(list) : [list];
    groups.forEach((group, gi) => {
      // the public servers of another group (discovery's IPv6 servers): a node of their own, named for them
      const part = stage.redundant && gi > 0;
      const v6 = group[0].group === 'internet_v6' || group.every((t) => String(t.address || '').includes(':'));
      const kind = part && v6 ? `${stage.kind} (IPv6)` : stage.kind;
      // a hop that only ignores ping, or an IPv6 target this host cannot probe, neither makes its node look down nor
      // adds its loss
      const heard = group.filter((t) => !quiet.has(t.target_id));
      const silent = !heard.length;
      const judged = silent ? group : heard;
      const worst = judged.reduce((a, b) => (RANK[b.status] > RANK[a.status] ? b : a));
      // redundant probes (public resolvers): down only when all are down, degraded when any is not up
      let status = worst.status;
      if (stage.redundant && judged.length > 1) {
        const up = judged.filter((t) => t.status !== 'down');
        status = !up.length ? 'down' : (judged.some((t) => t.status !== 'up') ? 'degraded' : 'up');
      }
      const run = stage.each && group.length > 1; // the folded ISP hops, or hops the owner grouped as one node
      // the Internet node is the median across its targets (as in the latency chart); a run of ISP hops shows its last
      // hop that answers; other nodes show their worst
      const rep = run ? ([...heard].reverse().find((t) => has(latestMs(t))) || worst) : worst;
      const fig = stage.redundant && group.length > 1 ? internetFigure(judged) : null;
      const loss = silent ? null : Math.max(...heard.map((t) => (has(t.loss_recent_pct) ? t.loss_recent_pct : t.loss_pct)));
      const recent = fig ? fig.spark : (rep.recent_latencies || []);
      const ignored = group.filter((t) => quiet.get(t.target_id) === IGNORES).length;
      const unprobed = group.length - heard.length - ignored;
      const runName = `${status === 'up' ? `${group.length} hops, ${heard.length < group.length ? `${heard.length} answer` : 'all answer'}` : `worst: ${worst.name}`}`
        + `${ignored ? ` · ${ignored} ignore${ignored > 1 ? '' : 's'} ping` : ''}${unprobed ? ` · ${unprobed} not probed (no IPv6)` : ''}`;
      // a node of several targets opens their history together (dialogs/target-history): the members with the names
      // this node gives them, and the rule of the node's figure (the median, or the last hop that answers)
      let history = null;
      if (fig || run) {
        const labels = fig ? null : shortLabels(live.map(withId));
        const names = fig ? memberNames(group.map(withId), live.map(withId)) : group.map((t) => labels[t.target_id] || t.name);
        const what = group[0].category === 'dns' ? 'DNS lookups' : 'public servers';
        history = {
          title: fig ? `${kind} · median of ${group.length} ${what}` : `${kind} · ${group.length} hops`,
          rule: fig ? 'median' : 'last', category: group[0].category,
          members: group.map((t, k) => ({ id: t.target_id, short: names[k], name: t.name }))
        };
      }
      nodes.push({
        key: `${stage.kind}:${each ? (run ? `group:${group[0].target_id}` : worst.target_id) : run ? 'run' : part ? `group:${group[0].group}` : ''}`, kind: stage.kind,
        label: each ? `${stage.kind} hop ${gi + 1}` : kind, icon: stage.icon, count: group.length,
        name: fig ? memberNames(group.map(withId), live.map(withId)).join(', ') : run ? runName : worst.name,
        title: fig ? `Median of ${group.map((t) => t.name).join(', ')}` : run ? group.map((t) => t.name).join('\n') : worst.name,
        target: (run && status === 'up' ? rep : worst).target_id, history, level: silent ? 'unknown' : STATE[status] || 'unknown', status, quiet: silent,
        quietWords: silent ? quietWords(quiet, group[0].target_id) : null,
        ms: fig ? fig.ms : (recent.length ? median(recent.slice(-60)) : (has(rep.last_ms) ? rep.last_ms : null)),
        loss, jitter: rep.jitter_ms, spark: recent.slice(-60)
      });
    });
  });
  return nodes;
}
