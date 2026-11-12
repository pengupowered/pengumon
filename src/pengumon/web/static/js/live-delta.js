// Rebuilds the live message's `recent_latencies` from the deltas the server sends on a `?delta=1` socket (live/ws.py).
// A target arrives either with its whole window (`recent_latencies`) or with `latency_tail`: the probes since the
// previous message, `latency_drop` (how many old values fall out) and `latency_len` (the length after). `sent`, the
// server's probe counter, ties a tail to the window held: held.sent + tail.length must equal the new `sent`. Anything
// else is a gap, and the caller asks the server for a full message. The panels never see the delta fields.

/** Makes a fresh store of the windows held: target id -> { window, sent }. */
export function newWindows() {
  return new Map();
}

/**
 * Turns `data` (a parsed message) into the form a full message has, in place, and remembers the windows.
 * Returns false (and leaves `held` as it was) when a target's tail does not follow what is held: the message is then
 * not usable and a full one is needed.
 */
export function rebuildWindows(data, held) {
  const targets = data.targets;
  if (!Array.isArray(targets)) return true;
  const next = new Map();
  const rebuilt = [];
  for (const t of targets) {
    let window = t.recent_latencies;
    if (!Array.isArray(window)) {
      const was = held.get(t.target_id);
      const tail = t.latency_tail;
      if (!was || !Array.isArray(tail) || was.sent + tail.length !== t.sent) return false;
      window = was.window.slice(t.latency_drop || 0).concat(tail);
      if (window.length !== t.latency_len) return false;
    }
    next.set(t.target_id, { window, sent: t.sent });
    const whole = { ...t, recent_latencies: window };
    delete whole.latency_tail;
    delete whole.latency_drop;
    delete whole.latency_len;
    rebuilt.push(whole);
  }
  data.targets = rebuilt;
  held.clear();
  next.forEach((v, k) => held.set(k, v));
  return true;
}
