// The part every device card shares, whatever the device is: status badge (icon + word), the coloured bar on top, an
// error block with what to do, the "reading N min old" chip when the readings stop, and when it was read. Used by
// panels/line.js and panels/router.js; nothing here knows a technology or a vendor.

import { escapeHtml, icon, setBadge, setText } from './util.js';
import { relayout } from './layout.js';

const STALE_FACTOR = 3; // a reading older than three poll intervals (at least 2 min) is stale

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

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

// Paints the shared part; returns true when the snapshot holds a reading the body can show.
export function paintCommon(card, refs, device, snap, okWord) {
  const [level, word] = statusOf(snap, okWord);
  card.dataset.status = level;
  refs.badge.hidden = !word;
  if (word) setBadge(refs.badge, level, word);
  const reading = !!snap && snap.status !== 'error' && snap.status !== 'unconfigured' && snap.status !== undefined;
  // error or not set up: say so and offer the fix; the last body stays, dimmed
  if (snap && (snap.status === 'error' || snap.status === 'unconfigured')) {
    const unset = snap.status === 'unconfigured';
    refs.error.className = unset ? 'empty' : 'empty st st-crit';
    refs.error.innerHTML = `${icon(unset ? 'lock' : 'warn')}<div><b>${unset ? 'Not set up yet' : `No reading from ${escapeHtml(device.host || device.id)}`}</b>${escapeHtml(snap.error_message || (unset ? 'Enter the admin password to start reading this device.' : 'The device did not answer.'))}<div class="row-btns"><button type="button" class="btn sm${unset ? ' primary' : ' outline'}" data-action="openDeviceSetup" data-needs-edit data-arg="${escapeHtml(device.id)}">${icon('settings')}${unset ? 'Set up' : 'Check the sign-in'}</button></div></div>`;
    refs.error.hidden = false;
  } else {
    refs.error.hidden = true;
  }
  card.classList.toggle('is-error', !!snap && snap.status === 'error');
  refs.body.classList.toggle('dim', !!snap && snap.status === 'error');
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

// Adds a card for one instance to its panel's section and keeps its age honest.
export function placeCard(section, card, refs, device) {
  card.dataset.device = device.id;
  refs.setup.dataset.arg = device.id;
  refs.setup.setAttribute('aria-label', `Set up ${device.host || device.id}`);
  section.appendChild(card);
  setInterval(() => paintAge(card, refs, device), 30000);
  relayout();
}
