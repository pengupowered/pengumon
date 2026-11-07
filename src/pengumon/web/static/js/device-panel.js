// The part every device card shares, whatever the device is: status badge (icon + word), the coloured bar on top, an
// error block with what to do, the "reading N min old" chip when the readings stop, and when it was read. Used by
// panels/line.js and panels/router.js; nothing here knows a technology or a vendor.

import { formatClock, icon, setBadge, setText } from './util.js';
import { relayout } from './layout.js';

const STALE_FACTOR = 3; // a reading older than three poll intervals (at least 2 min) is stale

const clock = (ts) => formatClock(ts * 1000);

function ago(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

// What a snapshot's status means on the card: [level, word]; `okWord` is the word for a healthy device (null: no badge).
export function statusOf(snap, okWord) {
  switch (snap && snap.status) {
    case 'healthy': return ['good', okWord];
    case 'warning': return ['warn', 'Warning'];
    case 'critical': return ['crit', 'Critical'];
    case 'error': return ['crit', 'No reading'];
    case 'unconfigured': return ['unknown', 'Not set up'];
    default: return ['unknown', 'Waiting'];
  }
}

// What a snapshot's error or missing set-up says, as the view model's `error` part (null: nothing to say): {tone:
// 'unset' | 'crit', icon, title, text, setup: the fix as an action descriptor or null}. The card's views draw it with
// js/device-view.js (errorPart); the body behind it stays, dimmed.
export function errorOf(snap, device) {
  if (!snap || (snap.status !== 'error' && snap.status !== 'unconfigured')) return null;
  const unset = snap.status === 'unconfigured';
  return {
    tone: unset ? 'unset' : 'crit',
    icon: unset ? 'lock' : 'warn',
    title: unset ? 'Not set up yet' : `No reading from ${device.host || device.id}`,
    text: snap.error_message || (unset ? 'Enter the admin password to start reading this device.' : 'The device did not answer.'),
    setup: device.needs_sign_in === false ? null
      : { action: 'openDeviceSetup', arg: device.id, needsEdit: true, label: unset ? 'Set up' : 'Check the sign-in', icon: 'settings', primary: unset }
  };
}

// Paints the shared frame (the badge, the stale chip, the read time, the card's status); returns true when the snapshot
// holds a reading the body can show. The error block and the dimmed body are the card's view model (errorOf).
export function paintCommon(card, refs, device, snap, okWord) {
  const [level, word] = statusOf(snap, okWord);
  card.dataset.status = level;
  refs.badge.hidden = !word;
  if (word) setBadge(refs.badge, level, word);
  const reading = !!snap && snap.status !== 'error' && snap.status !== 'unconfigured' && snap.status !== undefined;
  card.classList.toggle('is-error', !!snap && snap.status === 'error');
  card.dataset.readAt = snap && snap.timestamp ? String(snap.timestamp) : '';
  paintAge(card, refs, device);
  return reading;
}

// The read time and the stale chip (also re-checked every 30 s, as no new reading means no new snapshot).
export function paintAge(card, refs, device) {
  const ts = Number(card.dataset.readAt);
  if (!ts) {
    setText(refs.read, 'waiting for the first reading');
    refs.stale.hidden = true;
    card.classList.remove('is-stale');
    return;
  }
  const age = Date.now() / 1000 - ts;
  const limit = Math.max(120, STALE_FACTOR * (device.poll_interval_seconds || 30));
  const stale = age > limit;
  setText(refs.read, `read ${clock(ts)}${device.poll_interval_seconds ? ` · every ${ago(device.poll_interval_seconds)}` : ''}`);
  refs.stale.hidden = !stale;
  if (stale) refs.stale.innerHTML = `${icon('stale')}reading ${ago(age)} old`;
  card.classList.toggle('is-stale', stale);
}

// What a device card's model hands its card (cards.js createCard's handle): push(vm) draws `vm` unless no part of it
// changed, and a part that did not change keeps the object it had, so a view can skip it by identity. The JSON of the
// parts shown is kept per key, so a message costs one stringify per new part (readings come every few seconds).
export function partPusher(card) {
  let shown = null;
  const json = {};
  return (next) => {
    let changed = !shown;
    Object.keys(next).forEach((k) => {
      const s = JSON.stringify(next[k]);
      if (shown && s === json[k]) next[k] = shown[k];
      else { json[k] = s; changed = true; }
    });
    if (!changed) return;
    shown = next;
    card.update(next);
  };
}

// Adds a card for one instance to its panel's section and keeps its age honest. A device that needs no sign-in (UPnP)
// has nothing to set up: no Setup button, as its error block and the Settings list have none.
export function placeCard(section, card, refs, device) {
  card.dataset.device = device.id;
  refs.setup.hidden = device.needs_sign_in === false;
  refs.setup.dataset.arg = device.id;
  refs.setup.setAttribute('aria-label', `Set up ${device.host || device.id}`);
  section.appendChild(card);
  const age = () => paintAge(card, refs, device);
  setInterval(age, 30000);
  document.addEventListener('zonechange', age); // "read 14:02" is on the page's clock
  relayout();
}
