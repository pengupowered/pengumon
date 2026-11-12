// The probe path's API (api/path.py) and discovery's (POST /api/discovery, GET /api/discovery/{id}), for the path
// editor (dialogs/path-editor) and the first-run banner on Path health. Nothing here touches the page.

import { ROLES } from './roles.js';

// The message of a failed answer: FastAPI's detail (a string, or a list of {loc, msg}), else the status.
// A validation error's place in the editor's words: the server counts hops from 0 ("hops 7" is card 8) and calls the
// role "category".
const FIELD_WORD = { category: 'role', warn_latency_ms: 'warn above', device_id: 'device', probe: 'probe', port: 'port', http_scheme: 'scheme', http_path: 'path', http_verb: 'request' };
function whereText(loc) {
  const parts = (loc || []).filter((p) => p !== 'body');
  const words = [];
  for (let i = 0; i < parts.length; i++) {
    if (parts[i] === 'hops' && typeof parts[i + 1] === 'number') { words.push(`Hop ${parts[i + 1] + 1}`); i++; }
    else if (parts[i] !== 'hops') words.push(FIELD_WORD[parts[i]] || String(parts[i]).replace(/_/g, ' '));
  }
  return words.join(', ');
}
// "Input should be 'local', 'gateway' ..." lists the role codes; the editor calls them by their words.
const roleWords = (msg) => msg.replace(/Input should be ('\w+'(,| or)? ?)+/, () => `Pick a role: ${ROLES.map((r) => r[1]).join(', ')}`);

export async function errorText(res) {
  const body = await res.json().catch(() => ({}));
  const detail = body.detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      const where = whereText(d.loc);
      const msg = String(d.msg || '').replace(/^Value error, /, '');
      // only the role's own error lists roles: "Input should be 'icmp', 'tcp' or 'http'" is the probe's
      return `${where ? `${where}: ` : ''}${(d.loc || [])[(d.loc || []).length - 1] === 'category' ? roleWords(msg) : msg}`;
    }).join(' · ');
  }
  if (typeof detail === 'string') return detail;
  return res.status === 429 ? 'Too many requests in a minute. Wait a little and try again.' : `HTTP ${res.status}`;
}

export async function fetchPath() {
  const res = await fetch('/api/path');
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

// Starts a discovery (single-flight on the server: a second start returns the running one). With `firstRun` the
// server starts at most one per process and answers null once that run is gone. Its id, or an Error
// whose `unavailable` is true when this build has no discovery.
export async function startDiscovery({ asnLookup = true, snmp = false, firstRun = false } = {}) {
  const res = await fetch('/api/discovery', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(firstRun ? { asn_lookup: asnLookup, snmp, first_run: true } : { asn_lookup: asnLookup, snmp })
  });
  if (!res.ok) {
    const err = new Error(res.status === 404 || res.status === 405 ? 'Discovery is not available in this version.' : await errorText(res));
    err.unavailable = res.status === 404 || res.status === 405;
    throw err;
  }
  return (await res.json()).id;
}

// Polls a discovery until it is done or failed; `progress(text)` hears how far it is. The last answer
// ({state, progress, draft, notes}).
// It gives up after `limit` ms (a discovery that never ends must not keep a request a second going for the life of the page).
export async function waitDiscovery(id, progress = () => {}, every = 1000, limit = 600000) {
  const until = Date.now() + limit;
  for (;;) {
    if (Date.now() > until) throw new Error('Discovery is taking too long; try again in a while.');
    const res = await fetch(`/api/discovery/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(await errorText(res));
    const run = await res.json();
    if (run.state !== 'running') return run;
    progress(run.progress || 'Discovering…');
    await new Promise((r) => setTimeout(r, every));
  }
}

// Preview (POST /api/path/preview): the draft's hops not probed live are probed once a second for about 30 s, nothing
// recorded; a new one from this browser stops its last. {id, probed: [hop ids]}, or an Error whose `unavailable` is
// true when this build has none (429 while two others run: the server's words).
export async function startPreview(hops) {
  const res = await fetch('/api/path/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hops }) });
  if (!res.ok) {
    const err = new Error(res.status === 404 || res.status === 405 ? 'Preview is not available in this version.' : await errorText(res));
    err.unavailable = res.status === 404 || res.status === 405;
    throw err;
  }
  return res.json();
}

// One look at a preview: {state: 'running' | 'done', elapsed_s, hops: {id: {sent, received, loss_pct, median_ms, last_ms,
// kind: reply | timeout | unreachable | ipv6_unavailable}}}.
export async function readPreview(id) {
  const res = await fetch(`/api/path/preview/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

export function stopPreview(id) {
  return fetch(`/api/path/preview/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => null);
}

// The applied paths, newest first ([{revision, applied_at, hop_count, summary}]); null when this build keeps none.
export async function fetchVersions() {
  const res = await fetch('/api/path/versions');
  if (res.status === 404 || res.status === 405) return null;
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

export async function fetchVersion(revision) {
  const res = await fetch(`/api/path/versions/${encodeURIComponent(revision)}`);
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

// The last background check of the route ({checked_at, state, changes, discovery_id}); null when this build has none.
export async function fetchDrift() {
  const res = await fetch('/api/path/drift');
  if (res.status === 404 || res.status === 405) return null;
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

export async function dismissDrift() {
  const res = await fetch('/api/path/drift/dismiss', { method: 'POST' });
  if (!res.ok) throw new Error(await errorText(res));
}

// What happened to a hop ([{at, kind: 'address_changed', target_id, old, new, revision}], oldest first); [] when this build
// records none.
export async function fetchPathEvents(targetId) {
  const res = await fetch(`/api/path/events?target_id=${encodeURIComponent(targetId)}`);
  if (res.status === 404 || res.status === 405) return [];
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}
