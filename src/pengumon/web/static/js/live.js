// The live socket: parses each message from the server and publishes it on the bus, one topic per top-level key of
// the message and the whole message as `message`. What the page does with them is up to the
// panels that subscribe; nothing here knows them.

import { bus } from './bus.js';
import { sessionEnded } from './session.js';

let ws;
let received = false; // any message since the page loaded

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
        : '<b>Cannot reach pengumon.</b> No data yet; trying again every 2 s.';
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
    Object.entries(data).forEach(([topic, value]) => {
      if (value !== undefined) bus.publish(topic, value);
    });
    bus.publish('message', data);
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
