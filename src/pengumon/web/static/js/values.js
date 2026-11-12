// Reading numbers out of the server's answers: whether a value is there, the newest round trip, the median.

export const has = (v) => v !== null && v !== undefined;

// A live target's latest probe: its newest round trip, or null when that probe got no answer. Not last_ms, which keeps the
// last answer through an outage and would show a figure for a target that is down.
export const latestMs = (t) => { const r = t.recent_latencies; return r && r.length ? r[r.length - 1] : null; };

// The middle of the numbers in `values` (null and undefined skipped), the mean of the two middle ones when there is an even
// number (as Python's statistics.median); null for none.
export function median(values) {
  const v = values.filter((x) => x !== null && x !== undefined).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
