// Where each kind of data starts (GET /api/oldest): the oldest stored time of probes, router, mesh, line, traffic and
// speedtest data, so a chart at "All" starts at its first row and asks for its history once (js/ranges.js loadFromOldest;
// the Internet use card, panels/traffic/model.js). Read when first needed, kept for an hour, and read again after a retention
// change (a pruning moves the oldest row). A failed read is not kept: the charts then ask the old way, twice.

const KEEP_MS = 3600000;
let cached = null; // { at, answer: Promise<object|null> }

const read = async () => {
  try {
    const res = await fetch('/api/oldest');
    return res.ok ? await res.json() : null;
  } catch { return null; }
};

// Epoch seconds of the oldest stored row of `kind` (the retention kinds: probes, router, mesh, line, traffic, speedtest);
// null when nothing is stored or the server did not say.
export async function oldestStored(kind) {
  if (!cached || Date.now() - cached.at > KEEP_MS) cached = { at: Date.now(), answer: read() };
  const answer = await cached.answer;
  if (!answer) { cached = null; return null; }
  const t = answer[kind];
  return typeof t === 'number' ? t : null;
}

document.addEventListener('settingsvalues', () => { cached = null; });
