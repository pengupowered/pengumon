// The incident list the panel works from. It is loaded by time: one request for every incident that overlaps the range the
// card shows (GET /api/incidents?since=, in the light field set that leaves out the path traces: the inspector reads those),
// and one for the outages of the same stretch (GET /api/outages), in parallel. A longer range later (the timeline's 90 d, a
// chart range, a time window opened from another panel) asks only for the older part and joins it on. The server answers
// newest first and at most RANGE_MAX rows a request; a range with more answers its newest, and the pager (Next, Last) asks
// for the rest with offset, up to CAP incidents in all.
// Reloads (a new incidents_version, a reconnect) read the whole stretch loaded so far in one request, so the list never has
// a gap or a duplicate where an incident was added. Calls are queued: one request at a time (a reload asked for while one is
// queued gets that one).
//
// The outages (the drops of the line that belong together) are read for the same stretch of time as the incidents loaded. A
// group of drops is shown whole or not at all, so a load that leaves an outage with drops not loaded yet (one that straddles
// the start of the range) goes on loading older incidents until they are (or the cap, or the end of the list).

const PAGE = 50;
const RANGE_MAX = 5000; // what one request answers (api/incidents.py RANGE_MAX)
export const CAP = RANGE_MAX; // the most incidents the list holds (the server's offset reaches this far)

let list = [];
let exhausted = false; // the server had no older incident when last asked
let totals = null; // {total, expected}: the server's count of all incidents, not only the ones loaded (null if unknown)
let outages = null; // the outages of the stretch loaded (GET /api/outages), null when the server does not say
let outageHours = 0; // how far back `outages` reach, in hours
let want = null; // epoch seconds: the list reaches back to here (null: not asked for a range yet, the newest page only)
let sink = () => {};
let queue = Promise.resolve();
let waiting = 0;

// Incidents that overlap [since, until] newest first, `offset` newer ones skipped. since 0: the newest of all (a page).
const get = async (since, limit, { until = null, offset = 0 } = {}) => {
  const q = `since=${Math.floor(since)}&limit=${limit}&fields=list${until === null ? '' : `&until=${Math.ceil(until)}`}${offset ? `&offset=${offset}` : ''}`;
  const res = await fetch(`/api/incidents?${q}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

// The real number of incidents (GET /api/incidents/count); null when the server does not say.
const counts = async () => {
  try {
    const res = await fetch('/api/incidents/count');
    return res.ok ? await res.json() : null;
  } catch { return null; }
};

// The outages that overlap the last `hours` (at least a week); null on any failure (the card then lists incidents alone).
const readOutages = async (hours) => {
  try {
    const res = await fetch(`/api/outages?hours=${hours}&limit=5000`);
    if (!res.ok) return null;
    const body = await res.json();
    return Array.isArray(body.outages) ? body.outages : null;
  } catch { return null; }
};
const hoursBack = (ts) => Math.max(168, Math.ceil((Date.now() / 1000 - ts) / 3600) + 1);

const run = (fn) => {
  waiting += 1;
  const done = queue.then(fn).catch(console.error).finally(() => { waiting -= 1; });
  queue = done;
  return done;
};

// Where loaded incidents go: called with the whole list after every change.
export function bindIncidents(onChange) { sink = onChange; }

// Are there incidents the list has not loaded (and may load)? Is a request in flight or queued?
export const hasMore = () => !exhausted && list.length < CAP;
export const isLoading = () => waiting > 0;
export const totalCounts = () => totals;
export const loadedOutages = () => outages;
// Does an outage of 2 or more drops have a drop that is not loaded?
const incomplete = (loaded = list, found = outages) => {
  if (!found) return false;
  const have = new Set(loaded.map((i) => i.id));
  return found.some((o) => o.drops >= 2 && o.incident_ids.some((id) => !have.has(id)));
};
export const oldestLoaded = () => (list.length ? list[list.length - 1].start_time : null);

// Joins rows onto the list (newest first, no id twice).
const merge = (rows) => {
  const have = new Set(list.map((i) => i.id));
  list = list.concat(rows.filter((i) => !have.has(i.id))).sort((a, b) => b.start_time - a.start_time || b.id - a.id);
};
// Everything the list holds is within the range asked for: a page read past the range moves its start back.
const reach = () => { if (list.length && want !== null) want = Math.min(want, oldestLoaded()); };

// The first load and every refresh: everything loaded so far, in one request. `start` (epoch seconds, optional): the list
// must reach back at least that far. A reload asked for while another one is queued and has not started gets that one: it
// will read the list after the request, so queueing a second full read would only repeat it (a line that flaps asks for
// one every few seconds).
let queuedReload = null;
export const reload = (start) => {
  if (typeof start === 'number') want = want === null ? start : Math.min(want, start);
  if (queuedReload) return queuedReload;
  queuedReload = run(async () => {
    queuedReload = null; // started: a request from now on needs a read of its own
    await readAll();
  });
  return queuedReload;
};

const readAll = async () => {
  const since = want === null ? 0 : want;
  const limit = want === null ? Math.max(PAGE, list.length) : RANGE_MAX;
  const hours = hoursBack(want === null ? Date.now() / 1000 - 168 * 3600 : want);
  let rows;
  [rows, totals, outages] = await Promise.all([get(since, Math.min(limit, CAP)), counts(), readOutages(hours)]);
  outageHours = hours;
  list = rows;
  const asked = Math.min(limit, CAP);
  exhausted = totals ? list.length >= totals.total : want === null && rows.length < asked;
  if (rows.length >= asked) reach(); // a full answer: older ones may be left, so the range now starts at the oldest got
  sink(list);
  await fill();
};

// The next `size` incidents older than the list holds; false when there are none.
const more = async (size) => {
  const limit = Math.min(size, CAP - list.length);
  if (exhausted || limit <= 0) return false;
  const rows = await get(0, limit, { offset: list.length });
  merge(rows);
  exhausted = rows.length < limit;
  reach();
  await outagesFor();
  sink(list);
  return rows.length > 0;
};

// The outages reach back as far as the list (read again only when the list grew past them).
const outagesFor = async () => {
  const oldest = oldestLoaded();
  if (oldest === null || hoursBack(oldest) <= outageHours) return;
  const hours = hoursBack(oldest);
  const found = await readOutages(hours);
  outages = found;
  outageHours = hours;
};

// Older pages until no outage has a drop missing.
const fill = async () => {
  while (hasMore() && incomplete()) {
    if (!(await more(PAGE))) break;
  }
};

// One more page.
export const loadMore = () => run(async () => { await more(PAGE); await fill(); });
// Everything up to the cap, a request of RANGE_MAX at a time.
export const loadAll = () => run(async () => {
  while (hasMore()) if (!(await more(RANGE_MAX))) break; // (more of everything: no drop is missing at the cap but the end)
});
// The list reaches back to `ts` (epoch seconds) or the oldest incident there is: one request for what is older than
// the list holds now (and the outages of that stretch), none when it reaches that far already.
export const loadSince = (ts) => run(async () => {
  if (want === null) { want = ts; await readAll(); return; }
  if (ts < want && !exhausted) {
    const rows = await get(ts, RANGE_MAX, { until: want });
    merge(rows);
    want = rows.length >= RANGE_MAX ? Math.min(want, oldestLoaded()) : ts;
    if (list.length >= totalsOrInf()) exhausted = true;
    await outagesFor();
    sink(list);
  }
  await fill();
});
const totalsOrInf = () => (totals ? totals.total : Infinity);
