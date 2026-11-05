// The live socket: parses each message from the server and publishes it on the bus, one topic per top-level key of
// the message and the whole message as `message`. What the page does with them is up to the
// panels that subscribe; nothing here knows them.

import { bus } from './bus.js';
import { sessionEnded } from './session.js';

let ws;
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

function publishAll(data) {
  Object.entries(data).forEach(([topic, value]) => {
    if (value !== undefined) bus.publish(topic, value);
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

export function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  ws = new WebSocket(wsUrl);
  let opened = false;
  const status = document.getElementById('wsStatus');
  const statusText = document.getElementById('wsText');
  // Grey "Connecting" until the socket is open, never a green badge before there is data.
  const show = (state, text) => {
    status.className = `live live-${state}`;
    statusText.textContent = text;
  };

  // The page says so when the data stops arriving: body and topbar carry data-conn="lost" (CSS greys the verdict
  // pill and dims the cards) and the banner under the header explains. "Connecting" before the first message is grey too.
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

  ws.onopen = () => {
    opened = true;
    conn(false);
    show('live', 'Live · 1 s');
    bus.publish('connection', 'open');
  };

  ws.onmessage = (event) => {
    received = true;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      console.error('Error parsing telemetry:', e);
      return;
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

  ws.onclose = (event) => {
    // 1008: the server closed it because the sign-in ended (homeguard). A socket refused before it opened cannot
    // say why; one request tells (session.js turns a refused sign-in into the sign-in page; a down server just fails).
    if (event.code === 1008) {
      sessionEnded();
      return;
    }
    if (!opened) fetch('/api/info', { cache: 'no-store' }).catch(() => {});
    conn(true);
    show('connecting', 'Reconnecting');
    bus.publish('connection', 'closed');
    setTimeout(connectWebSocket, 2000);
  };

  ws.onerror = () => {
    ws.close();
  };
}
