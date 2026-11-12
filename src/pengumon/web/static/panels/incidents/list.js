// The incident list the panel works from, loaded in pages: the newest 50 at start, older ones when something needs them
// (the 7-day timeline, a time window opened from another panel, a search, the pager's last page, a custom chart range),
// up to 500 in all (what the page always held). The server answers newest first (GET /api/incidents?limit=&offset=).
// Reloads (a new incidents_version, a reconnect) fetch what is loaded so far in one request, so the list never has a gap
// or a duplicate where an incident was added. Calls are queued: one request at a time.
//
// The outages (GET /api/outages, the drops of the line that belong together) are read for the same stretch of time as
// the incidents loaded, with every load. A group of drops is shown whole or not at all, so a load that leaves an outage
// with drops not loaded yet goes on loading older incidents until they are (or the cap, or the end of the list).

const PAGE = 50;
export const CAP = 500;

let list = [];
let exhausted = false; // the server had no older incident when last asked
let totals = null; // {total, expected}: the server's count of all incidents, not only the ones loaded (null if unknown)
let outages = null; // the outages of the stretch loaded (GET /api/outages), null when the server does not say
let sink = () => {};
let queue = Promise.resolve();
let waiting = 0;

const get = async (limit, offset) => {
  const res = await fetch(`/api/incidents?limit=${limit}${offset ? `&offset=${offset}` : ''}`);
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

// The outages that overlap the time the loaded incidents reach back to (at least a week); null on any failure (the card
// then lists incidents alone).
const readOutages = async () => {
  const oldest = list.length ? list[list.length - 1].start_time : null;
  const back = oldest === null ? 168 : Math.ceil((Date.now() / 1000 - oldest) / 3600) + 1;
  try {
    const res = await fetch(`/api/outages?hours=${Math.min(10000, Math.max(168, back))}&limit=500`);
    if (!res.ok) return null;
    const body = await res.json();
    return Array.isArray(body.outages) ? body.outages : null;
  } catch { return null; }
};

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

// The first load and every refresh: everything loaded so far, one page at least.
// A reload asked for while another one is queued and has not started gets that one: it will read the list after the
// request, so queueing a second full read would only repeat it (a line that flaps asks for one every few seconds).
let queuedReload = null;
export const reload = () => {
  if (queuedReload) return queuedReload;
  queuedReload = run(async () => {
    queuedReload = null; // started: a request from now on needs a read of its own
    await readAll();
  });
  return queuedReload;
};

const readAll = async () => {
  const limit = Math.min(CAP, Math.max(PAGE, list.length));
  [list, totals] = await Promise.all([get(limit, 0), counts()]);
  exhausted = list.length < limit;
  outages = await readOutages();
  sink(list);
  await fill();
};

const more = async (size) => {
  const limit = Math.min(size, CAP - list.length);
  if (exhausted || limit <= 0) return false;
  const rows = await get(limit, list.length);
  const have = new Set(list.map((i) => i.id));
  list = list.concat(rows.filter((i) => !have.has(i.id)));
  exhausted = rows.length < limit;
  outages = await readOutages();
  sink(list);
  return rows.length > 0;
};

// Older pages until no outage has a drop missing.
const fill = async () => {
  while (hasMore() && incomplete()) {
    if (!(await more(PAGE))) break;
  }
};

// One more page.
export const loadMore = () => run(async () => { await more(PAGE); await fill(); });
// Everything up to the cap.
export const loadAll = () => run(() => more(CAP)); // (more of everything: no drop is missing at the cap but the end)
// Pages until the oldest loaded incident started before `ts` (epoch seconds), or there are no more.
export const loadSince = (ts) => run(async () => {
  while (hasMore() && (!list.length || oldestLoaded() >= ts)) {
    if (!(await more(PAGE))) break;
  }
  await fill();
});
