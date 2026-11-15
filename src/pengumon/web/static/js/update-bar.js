// The update bar: an open dashboard tab notices that the server was upgraded and offers to reload. The page notes the
// version GET /api/health answers at load, asks again whenever the live socket reconnects (a restart) and every 10 minutes,
// and when it differs shows a bar across the top, "Pengumon was updated to X.Y.Z. Reload to use it." with a Reload button
// and a close, which hides it until the version changes again. (A tab opened before an upgrade keeps the old page's
// scripts, so it can show a card the new server has changed: the owner saw 0.27's latency card after the 0.28 deploy.)

import { bus } from './bus.js';
import { registerActions } from './dispatch.js';
import { everyWhileVisible } from './timers.js';

const CHECK_MS = 600000;
let known = null; // the version the page was loaded with
let dismissed = null; // the newer version the person closed the bar for
let checking = null;

async function health() {
  try {
    const res = await fetch('/api/health', { cache: 'no-store' });
    if (!res.ok) return null;
    const body = await res.json();
    return typeof body.version === 'string' && body.version ? body.version : null;
  } catch (e) {
    return null; // a restart in progress, or no connection: the next check will tell
  }
}

function show(version) {
  const bar = document.getElementById('updateBar');
  if (!bar) return;
  bar.querySelector('[data-f="version"]').textContent = version;
  bar.hidden = false;
}

// Asks the server for its version now; one question at a time.
export function checkVersion() {
  if (checking) return checking;
  checking = (async () => {
    const version = await health();
    if (version === null) return;
    if (known === null) { known = version; return; }
    const bar = document.getElementById('updateBar');
    if (version === known) { if (bar) bar.hidden = true; return; }
    if (version !== dismissed) show(version);
  })().finally(() => { checking = null; });
  return checking;
}

export function initUpdateBar() {
  checkVersion();
  bus.subscribe('connection', (state) => { if (state === 'open') checkVersion(); });
  everyWhileVisible(checkVersion, CHECK_MS);
}

registerActions('click', {
  reloadPage: () => window.location.reload(),
  closeUpdateBar: () => {
    const bar = document.getElementById('updateBar');
    if (!bar) return;
    dismissed = bar.querySelector('[data-f="version"]').textContent;
    bar.hidden = true;
  }
});
