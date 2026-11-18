// Previewing the dashboard without a device (pengumon/preview.py): this browser shows the dashboard as a household without
// the router or the modem (or with the router as a basic UPnP one) would see it. The choice is the cookie pengumon_preview
// ("router:absent,modem:basic"), which every request and the live stream carry, so the server shapes each answer for this
// browser alone; nothing stored changes and the devices are still read. Settings > Display chooses it (startPreview); the
// bar across the top says so while it is on and ends it. The device labels for the bar are kept in this browser too.

import { registerActions } from './dispatch.js';

export const COOKIE = 'pengumon_preview';
const LABELS = 'pengumon.previewLabels';
const YEAR = 365 * 86400;
export const MODES = { absent: 'not there', basic: 'as a basic UPnP router' };

// The preview now: {deviceId: 'absent' | 'basic'} ({} when none). Entries the server would not take are left out here too.
export function currentPreview() {
  const raw = document.cookie.split(';').map((c) => c.trim()).find((c) => c.startsWith(`${COOKIE}=`));
  const out = {};
  if (!raw) return out;
  decodeURIComponent(raw.slice(COOKIE.length + 1)).split(',').forEach((part) => {
    const m = /^([A-Za-z0-9_-]{1,64}):(absent|basic)$/.exec(part.trim());
    if (m) out[m[1]] = m[2];
  });
  return out;
}

function labels() {
  try { return JSON.parse(localStorage.getItem(LABELS) || '{}') || {}; } catch (e) { return {}; }
}

// Starts (or changes) the preview: `modes` {deviceId: 'absent' | 'basic'}, `names` {deviceId: 'Example router'} for the bar.
// An empty `modes` ends it. The page is loaded again, so every card asks the server anew.
export function startPreview(modes, names = {}) {
  const entries = Object.entries(modes).filter(([, m]) => m in MODES);
  if (entries.length) {
    document.cookie = `${COOKIE}=${encodeURIComponent(entries.map(([id, m]) => `${id}:${m}`).join(','))}; Path=/; Max-Age=${YEAR}; SameSite=Strict`;
    try { localStorage.setItem(LABELS, JSON.stringify(names)); } catch (e) { /* the bar names the ids instead */ }
  } else {
    document.cookie = `${COOKIE}=; Path=/; Max-Age=0; SameSite=Strict`;
    try { localStorage.removeItem(LABELS); } catch (e) { /* nothing kept */ }
  }
  window.location.reload();
}

// "without the Example modem (modem), with the Example router (router) as a basic UPnP router": what the bar says.
export function previewWords(modes, names = labels()) {
  const name = (id) => (names[id] ? `the ${names[id]} (${id})` : id);
  const and = (list) => (list.length > 1 ? `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}` : list[0]);
  const absent = Object.keys(modes).filter((id) => modes[id] === 'absent').map(name);
  const basic = Object.keys(modes).filter((id) => modes[id] === 'basic').map(name);
  return [absent.length ? `without ${and(absent)}` : '', basic.length ? `with ${and(basic)} as a basic UPnP router` : '']
    .filter(Boolean).join(', ');
}

// The bar across the top while a preview is on.
export function initPreviewBar() {
  const bar = document.getElementById('previewBar');
  const modes = currentPreview();
  if (!bar || !Object.keys(modes).length) return;
  bar.querySelector('[data-f="devices"]').textContent = previewWords(modes);
  bar.hidden = false;
}

registerActions('click', {
  endDevicePreview: () => startPreview({})
});
