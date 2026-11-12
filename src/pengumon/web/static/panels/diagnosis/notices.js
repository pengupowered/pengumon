// The notices over Path health's chain: the first run's ("Finding your network path", then "Review") and the route-change
// one ("Your path changed", a failed check's standing line). The card's model hands in its push (bindNotices), the notice
// is a `banner` part of the view model; model.js decides when to ask (firstRun, and driftNotice every few minutes).

import { registerActions } from '../../js/dispatch.js';
import { localFormat } from '../../js/clock.js';
import { accessKnown, canEdit } from '../../js/view-only.js';
import { dismissDrift, fetchDrift, startDiscovery, waitDiscovery } from '../../js/path-api.js';

let push = () => {};
export function bindNotices(pushPart) { push = pushPart; }

// A notice: { level, icon, title, text, progress (the text is the discovery's progress), actions: [{action, arg, label,
// primary, needsEdit}] }; null for none.
const review = (arg, title, text) => ({
  level: 'unknown', icon: 'path', title, text, progress: false,
  actions: [{ action: 'openPathEditor', arg, label: 'Review', primary: true, needsEdit: true }]
});
let notice = ''; // which notice shows: 'first' (the first run's) or 'drift'


// First run: discovery starts by itself and the notice offers its draft for review (see firstRun in model.js).
export async function firstRunBanner() {
  await accessKnown; // the sign-in state first
  if (!canEdit()) { push({ banner: null }); return; } // a viewer without the token: discovery and applying a path are the owner's
  notice = 'first';
  const finding = (text) => push({ banner: { level: 'unknown', icon: 'route', title: 'Finding your network path…', text, progress: true, actions: [] } });
  finding('Tracing the hops to the internet.');
  try {
    const id = await startDiscovery({ asnLookup: false, snmp: false, firstRun: true });
    const run = id ? await waitDiscovery(id, finding) : null;
    if (run && run.state === 'done') {
      const homeFound = ((run.draft && run.draft.hops) || []).some((h) => h.category === 'local' || h.category === 'gateway');
      push({ banner: homeFound
        ? review(`discovery:${id}`, 'Pengumon found your network path', '· Review it: until you apply it, the default targets are probed.')
        : review(`discovery:${id}`, 'Pengumon could not find your router', '· Only public servers answered. Review the draft: until you apply a path, the default targets are probed.') });
      return;
    }
  } catch (_) { /* no discovery in this build, or it failed: the defaults can be reviewed all the same */ }
  push({ banner: review('', 'Review your network path', '· The default targets are probed until you apply a path of your own.') });
}

// The route changed (GET /api/path/drift: discovery reruns quietly, daily and after an incident): "Your path changed ·
// Review" opens the editor with that discovery's draft; Dismiss marks these changes seen. Checked at the start and
// every few minutes; a build without the check shows nothing.
export const DRIFT_EVERY_MS = 5 * 60 * 1000;
let driftKey = '';

// "03:00", or "5 Nov, 03:00" when it was not today.
function checkTime(sec) {
  const ms = sec * 1000;
  const today = localFormat(Date.now(), { day: 'numeric', month: 'short' }) === localFormat(ms, { day: 'numeric', month: 'short' });
  return localFormat(ms, today ? { hour: '2-digit', minute: '2-digit' } : { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// Why a check failed, in words (the server's `error` says it in its own: no addresses in either).
function failureReason(error) {
  const e = String(error || '');
  if (/^No trace reached/i.test(e)) return 'no trace reached the internet, so the connection may have been down';
  if (/^Discovery failed/i.test(e)) return 'the route discovery hit an error';
  return 'the check could not run';
}

// The standing line while the latest background check failed: when, why, and which check the path shown comes from.
function failedCheckLine(drift) {
  const at = drift.checked_at ? ` at ${checkTime(drift.checked_at)}` : '';
  const from = drift.good_at ? `the path shown is from ${checkTime(drift.good_at)}` : 'no earlier check has succeeded';
  return `The last route check failed${at} (${failureReason(drift.error)}); ${from}.`;
}

export function driftBanner(drift, edit) {
  const failed = drift.state === 'failed';
  const changes = drift.changes || [];
  if (!changes.length) { // only the failed check to tell
    return { level: 'unknown', icon: 'route', title: 'Route check', text: `· ${failedCheckLine(drift)}`, progress: false, actions: [] };
  }
  const first = String(changes[0].text || '').replace(/\.$/, '');
  const more = changes.length > 1 ? ` (and ${changes.length - 1} more)` : '';
  const actions = [{ action: 'openPathEditor', arg: `discovery:${drift.discovery_id}`, label: 'Review', primary: true, needsEdit: true }];
  if (edit) actions.push({ action: 'dismissPathDrift', label: 'Dismiss', needsEdit: true });
  return { level: 'warn', icon: 'route', title: 'Your path changed', text: `· ${first}${more}.${failed ? ` ${failedCheckLine(drift)}` : ''}`, progress: false, actions };
}

export async function driftNotice() {
  let drift;
  try { drift = await fetchDrift(); } catch (_) { return; }
  // a failed check keeps the changes of the last good one until they are reviewed
  const changes = drift && (drift.state === 'changed' || drift.state === 'failed') && drift.discovery_id ? (drift.changes || []) : [];
  if (!changes.length && !(drift && drift.state === 'failed')) {
    if (notice === 'drift') { notice = ''; push({ banner: null }); }
    return;
  }
  const key = `${drift.discovery_id}|${drift.checked_at}|${drift.state}`;
  if (key === driftKey && notice === 'drift') return;
  driftKey = key;
  notice = 'drift';
  push({ banner: driftBanner({ ...drift, changes }, canEdit()) });
}

async function dismiss(el) {
  el.disabled = true;
  try {
    await dismissDrift();
    notice = '';
    push({ banner: null });
  } catch (_) {
    el.disabled = false;
  }
}

registerActions('click', { dismissPathDrift: (el) => dismiss(el) });
// the route-change notice can name a time (the last good check): on the page's clock
document.addEventListener('zonechange', () => { if (notice === 'drift') { driftKey = ''; driftNotice(); } });
