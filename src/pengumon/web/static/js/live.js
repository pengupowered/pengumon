// The live socket: parses each message from the server and publishes it on the bus, one topic per top-level key of
// the message and the whole message as `message`. What the page does with them is up to the
// panels that subscribe; nothing here knows them.

import { bus } from './bus.js';
import { sessionEnded } from './session.js';
import { newWindows, rebuildWindows } from './live-delta.js';

let received = false; // any message since the page loaded

// A hidden tab does not need the picture: nobody sees the charts or the tables. While `document.hidden`
// a message is parsed, but only the topics that a subscriber asked to see in full (bus.subscribe(..., { every: true }):
// an alert on a status change, a list kept of the readings seen) are published, and only when their value changed.
// Everything else is a snapshot that the next message replaces or is built up from the per-second messages (the live
// chart), so only the newest whole message is kept, and when the tab shows again it is published once, followed by
// `tab:resumed` ({missed, at}) so that what builds history from the per-second messages can fetch what it missed.
let held = null; // the newest message not published (hidden tab)
let missed = 0; // how many messages were not published
const sent = new Map(); // topic -> JSON of what was last published while hidden

function changed(topic, value) {
  const now = JSON.stringify(value);
  const before = sent.has(topic) ? sent.get(topic) : JSON.stringify(bus.last(topic));
  sent.set(topic, now);
  return now !== before;
}

// The socket is opened with ?delta=1: the server sends each target's `recent_latencies` in full once and then only the
// probes since the last message; live-delta.js puts the whole window back before anything is published. A message that
// does not follow what is held (a gap) is dropped and the server is asked for a full one ({"resync": true}); if none
// comes in RESYNC_WAIT_MS (a proxy that does not pass what the page sends), the socket is closed and opened again,
// which starts with a full message.
const RESYNC_WAIT_MS = 4000;
let windows = newWindows();
let resyncTimer = null;

function askForFull(socket) {
  if (resyncTimer) return;
  try {
    socket.send('{"resync":true}');
  } catch (e) {
    socket.close();
    return;
  }
  resyncTimer = setTimeout(() => {
    resyncTimer = null;
    socket.close();
  }, RESYNC_WAIT_MS);
}

function publishAll(data) {
  Object.entries(data).forEach(([topic, value]) => {
    if (value === undefined) return;
    // a list (the targets, with 60 values each) is new every second; a scalar or an object that the server repeats as it was
    // is published when it changed (both repeat for minutes; a `timestamp` key does not count)
    if (!Array.isArray(value)) bus.publishIfChanged(topic, value, ['timestamp']);
    else bus.publish(topic, value);
  });
  bus.publish('message', data);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden || !held) return;
  const data = held;
  const n = missed;
  held = null;
  missed = 0;
  sent.clear();
  publishAll(data);
  bus.publish('tab:resumed', { missed: n, at: Date.now() });
});

// The server sends one message a second. A connection that dies without a close (a laptop that slept, Wi-Fi that
// roamed, a NAT or proxy that dropped the flow) never calls onclose for minutes, so the page watches the clock: after
// STALE_MS without a message the badge says "Stale", after DEAD_MS the socket is given up and opened again.
const STALE_MS = 6000;
const DEAD_MS = 10000;
const WATCH_MS = 2500;

export function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws?delta=1`;

  const status = document.getElementById('wsStatus');
  const statusText = document.getElementById('wsText');
  // Gray "Connecting" until the socket is open, never a green badge before there is data.
  const show = (state, text) => {
    status.className = `live live-${state}`;
    statusText.textContent = text;
  };

  // The page says so when the data stops arriving: body and topbar carry data-conn="lost" (CSS grays the verdict
  // pill and dims the cards) and the banner under the header explains. "Connecting" before the first message is gray too.
  const conn = (lost) => {
    document.body.dataset.conn = lost ? 'lost' : 'open';
    document.getElementById('topbar')?.setAttribute('data-conn', lost ? 'lost' : 'open');
    const banner = document.getElementById('connBanner');
    if (banner) {
      banner.hidden = !lost;
      banner.querySelector('span').innerHTML = received
        ? '<b>Connection lost.</b> Showing the last data; reconnecting every 2 s.'
        : '<b>Cannot reach Pengumon.</b> No data yet; trying again every 2 s.';
    }
  };

  let opened = false;
  let over = false; // this socket is done with: whatever it says later is ignored
  let lastAt = Date.now();
  let watchdog = null;
  let socket;
  const gaveUp = () => {
    over = true;
    clearInterval(watchdog);
    conn(true);
    show('connecting', 'Reconnecting');
    bus.publish('connection', 'closed');
    setTimeout(connectWebSocket, 2000);
  };

  try {
    socket = new WebSocket(wsUrl);
  } catch (e) { // (a SecurityError, say): try again like any other failure
    console.error('Cannot open the live socket:', e);
    gaveUp();
    return;
  }
  windows = newWindows();
  clearTimeout(resyncTimer);
  resyncTimer = null;

  const check = () => {
    if (over) return;
    const quiet = Date.now() - lastAt;
    if (opened && quiet >= DEAD_MS) {
      // close() on a dead connection can take a long while to answer, so do not wait for onclose
      try { socket.close(); } catch (e) { /* already gone */ }
      unwatch();
      gaveUp();
    } else if (opened && quiet >= STALE_MS) {
      show('connecting', 'Stale');
    }
  };
  watchdog = setInterval(check, WATCH_MS);
  const wake = () => check();
  document.addEventListener('visibilitychange', wake);
  window.addEventListener('online', wake);
  const unwatch = () => {
    document.removeEventListener('visibilitychange', wake);
    window.removeEventListener('online', wake);
  };

  socket.onopen = () => {
    if (over) return;
    opened = true;
    lastAt = Date.now();
    conn(false);
    show('live', 'Live · 1 s');
    bus.publish('connection', 'open');
  };

  socket.onmessage = (event) => {
    if (over) return;
    received = true;
    lastAt = Date.now();
    if (statusText.textContent === 'Stale') show('live', 'Live · 1 s');
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      console.error('Error parsing telemetry:', e);
      return;
    }
    const wasGap = !rebuildWindows(data, windows);
    if (wasGap) {
      askForFull(socket);
      return;
    }
    if (resyncTimer) {
      // a message that follows what is held: the full one asked for (later tails follow only after it)
      clearTimeout(resyncTimer);
      resyncTimer = null;
    }
    if (document.hidden) {
      Object.entries(data).forEach(([topic, value]) => {
        if (value !== undefined && bus.wantsEvery(topic) && changed(topic, value)) bus.publish(topic, value);
      });
      held = data;
      missed += 1;
      return;
    }
    publishAll(data);
  };

  socket.onclose = (event) => {
    unwatch();
    if (over) return;
    // 1008: the server closed it because the sign-in ended (homeguard). A socket refused before it opened cannot
    // say why; one request tells (session.js turns a refused sign-in into the sign-in page; a down server just fails).
    // While this browser changes its own sign-in the page stays (sessionEnded says no) and reconnects like any other close.
    if (event.code === 1008 && sessionEnded()) {
      over = true;
      clearInterval(watchdog);
      return;
    }
    if (!opened) fetch('/api/info', { cache: 'no-store' }).catch(() => {});
    gaveUp();
  };

  socket.onerror = () => {
    socket.close();
  };
}
