// The incident list the panel works from, loaded in pages: the newest 50 at start, older ones when something needs them
// (the 7-day timeline, a time window opened from another panel, a search, the pager's last page, a custom chart range),
// up to 500 in all (what the page always held). The server answers newest first (GET /api/incidents?limit=&offset=).
// Reloads (a new incidents_version, a reconnect) fetch what is loaded so far in one request, so the list never has a gap
// or a duplicate where an incident was added. Calls are queued: one request at a time.

export const PAGE = 50;
export const CAP = 500;

let list = [];
let exhausted = false; // the server had no older incident when last asked
let totals = null; // {total, expected}: the server's count of all incidents, not only the ones loaded (null if unknown)
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
export const oldestLoaded = () => (list.length ? list[list.length - 1].start_time : null);

// The first load and every refresh: everything loaded so far, one page at least.
export const reload = () => run(async () => {
  const limit = Math.min(CAP, Math.max(PAGE, list.length));
  [list, totals] = await Promise.all([get(limit, 0), counts()]);
  exhausted = list.length < limit;
  sink(list);
});

const more = async (size) => {
  const limit = Math.min(size, CAP - list.length);
  if (exhausted || limit <= 0) return false;
  const rows = await get(limit, list.length);
  const have = new Set(list.map((i) => i.id));
  list = list.concat(rows.filter((i) => !have.has(i.id)));
  exhausted = rows.length < limit;
  sink(list);
  return rows.length > 0;
};

// One more page.
export const loadMore = () => run(() => more(PAGE));
// Everything up to the cap.
export const loadAll = () => run(() => more(CAP));
// Pages until the oldest loaded incident started before `ts` (epoch seconds), or there are no more.
export const loadSince = (ts) => run(async () => {
  while (hasMore() && (!list.length || oldestLoaded() >= ts)) {
    if (!(await more(PAGE))) break;
  }
});
