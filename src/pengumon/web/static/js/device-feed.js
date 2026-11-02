// One bus topic per device, `device:<id>`, carrying that device's latest snapshot, whichever way it arrives:
//   * the live message's `devices` map (every enabled device by id: a summary with status, grades and the headline
//     figures, no detail);
//   * GET /api/devices/{id} (the full reading): once at start for every watched device, then every poll interval for a
//     device the live message has not carried for two minutes.
// A card that needs the detail (a line's per-unit readings) fetches it itself when the summary's timestamp changes.
// Device panels subscribe to their device's topic and never care which source it was.

const watched = new Map(); // id -> { device, liveAt }
let bus = null;

function publish(id, snapshot) {
  if (!snapshot || !watched.has(id)) return;
  bus.publish(`device:${id}`, snapshot);
}

function live(id, snapshot) {
  const w = watched.get(id);
  if (!w || !snapshot) return;
  w.liveAt = Date.now();
  publish(id, snapshot);
}

async function fetchOne(id) {
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(id)}`);
    if (res.ok) publish(id, await res.json());
  } catch (e) {
    console.warn(`device ${id}: no reading`, e);
  }
}

function poll() {
  const now = Date.now();
  watched.forEach((w, id) => {
    const every = Math.max(10, w.device.poll_interval_seconds || 30) * 1000;
    if (now - w.liveAt > 120000 && now - (w.polledAt || 0) >= every) {
      w.polledAt = now;
      fetchOne(id);
    }
  });
}

function start(b) {
  if (bus) return;
  bus = b;
  b.subscribe('devices', (map) => Object.entries(map || {}).forEach(([id, snap]) => live(id, snap)));
  setInterval(poll, 5000);
}

// Starts the feed for `device` (idempotent) and calls `fn` with each snapshot; returns the unsubscribe function.
export function watchDevice(b, device, fn) {
  start(b);
  if (!watched.has(device.id)) {
    watched.set(device.id, { device, liveAt: 0, polledAt: Date.now() });
    fetchOne(device.id);
  }
  return b.subscribe(`device:${device.id}`, fn);
}

// A snapshot that came from elsewhere (the setup dialog's test poll) goes to the device's panels too.
export function publishDevice(id, snapshot) {
  if (bus) publish(id, snapshot);
}

