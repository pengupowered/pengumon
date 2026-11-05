// The probe path's API (api/path.py) and discovery's (POST /api/discovery, GET /api/discovery/{id}), for the path
// editor (dialogs/path-editor) and the first-run banner on Path health. Nothing here touches the page.

import { statusOf } from './device-panel.js';

// The roles a hop can have (TargetConfig.category), nearest first: the one table of their words. `short` is the name a
// panel gives a hop of that role (Router, ISP hop 2); `long` is the role in the editor's plain words (Your network).
// Every place that says a role, a stage or a category reads it from here.
export const ROLE_TABLE = {
  local: { short: 'Router', long: 'Your network' },
  gateway: { short: 'Gateway', long: 'Gateway (modem)' },
  isp: { short: 'ISP hop', long: 'Provider' },
  public: { short: 'Internet', long: 'Internet' },
  dns: { short: 'DNS lookup', long: 'DNS server' },
  custom: { short: 'Other', long: 'Other' }
};
export const ROLES = Object.entries(ROLE_TABLE).map(([id, w]) => [id, w.long]); // [id, long word] for a select
export const ROLE_WORD = Object.fromEntries(ROLES);
export const ROLE_SHORT = Object.fromEntries(Object.entries(ROLE_TABLE).map(([id, w]) => [id, w.short]));

// Where an incident's fault sat (the server's `cause` category), in the timeline's lane order: `short` is the lane and
// the chip, `long` says what it means in the report. Router (local probes), Gateway (the gateway / modem on its home-side
// address, which does not use the line), Line (loss from the provider's first router on while the gateway answers: the
// line or that router), ISP (the provider's network beyond it).
export const CAUSES = [
  ['router', 'Router', 'Router: home network'],
  ['gateway', 'Gateway', 'Gateway (modem)'],
  ['line', 'Line', 'Line or first ISP router'],
  ['isp', 'ISP', 'ISP: beyond its first router'],
  ['internet', 'Internet', 'Internet servers'],
  ['dns', 'DNS', 'DNS'],
  ['load', 'Load', 'Busy line'],
  ['service', 'Service', 'Host service'],
  ['unknown', 'Other', 'Unknown']
];
export const CAUSE_SHORT = Object.fromEntries(CAUSES.map(([id, short]) => [id, short]));
export const CAUSE_LONG = Object.fromEntries(CAUSES.map(([id, , long]) => [id, long]));
export const ROLE_HINT = {
  local: 'router or LAN device', gateway: 'modem or the provider\'s gateway in your home', isp: 'a router of your provider',
  public: 'a server on the internet', dns: 'a DNS server', custom: 'not a stage of the path (a NAS, a VPN, a server)'
};

// What each role changes, in one plain line, shown where the role is picked (the classifier's rules: docs/diagnosis.md).
export const ROLE_EFFECT = {
  local: 'If this stops answering, the outage is placed in your home network (router or Wi-Fi), not on your provider; the provider dossier quotes its loss to show your own network is fine.',
  gateway: 'Probed on its home-side address, so these pings never use the line: loss here while your router is fine is placed at the gateway itself (or the link to it), not on the line; loss at the router before it is yours.',
  isp: 'Loss only here, while later hops answer, is not counted as packet loss (provider routers deprioritise ping); loss that carries on to the internet is your provider\'s.',
  public: 'Every stage before it fine but this failing is an internet outage; the Internet figure on Path health is the median of these servers.',
  dns: 'Failures here are reported as a DNS problem, not as loss on the path.',
  custom: 'Probed and charted, but never blamed for an outage and left out of the dossier\'s findings.'
};

// A configured device as a person names it (/api/devices `label`: what it is and its model), never only an address.
export const deviceLabel = (d) => d.label || (d.model ? String(d.model).replace(/_/g, ' ') : d.id);

// The host of a device's URL ("http://192.0.2.1:8080/" -> "192.0.2.1"; an IPv6 host without its brackets).
export function deviceHost(d) {
  try {
    return new URL(d.url).hostname.replace(/^\[|\]$/g, '').toLowerCase();
  } catch (_) {
    return String(d.host || '').toLowerCase();
  }
}

// The roles whose hops can be a configured device (engine/device_links.py): your network and the gateway.
export const DEVICE_ROLES = ['local', 'gateway'];

// "Hop 3: Provider router" -> "Provider router": the numbered prefix older configs carry (Path health numbers hops).
const HOP_PREFIX = /^\s*hop\s*\d+\s*[:.)\u2013\u2014-]\s*/i;
export function cleanName(name) {
  const rest = String(name || '').replace(HOP_PREFIX, '');
  return rest && rest !== name ? rest : null;
}

// A device in one line, from its latest snapshot (the live summary or GET /api/devices/{id}), for the places that name a
// device without its card (the editor's device line, a Path health node's tooltip): {level, word, figures, panel,
// panelName}. The word is the card's ("Healthy" for a router, "Line up" for a line, js/device-panel.js statusOf), the
// figures the card's headline ones: CPU and clients for a router; for a DOCSIS line its downstream SNR and channels.
export function deviceStatus(device, snap) {
  const line = (device.offers || []).includes('access_line');
  const [level, word] = statusOf(snap, line ? 'Line up' : 'Healthy');
  const figures = [];
  const has = (v) => typeof v === 'number' && Number.isFinite(v);
  if (snap && snap.status !== 'error' && snap.status !== 'unconfigured') {
    if (has(snap.cpu_usage_pct)) figures.push(`CPU ${Math.round(snap.cpu_usage_pct)}%`);
    const clients = snap.clients && has(snap.clients.online) ? snap.clients.online : snap.client_count;
    if (has(clients)) figures.push(`${clients} client${clients === 1 ? '' : 's'}`);
    if (line && device.line_technology === 'docsis') {
      if (has(snap.downstream_snr_avg)) figures.push(`SNR ${snap.downstream_snr_avg.toFixed(1)} dB`);
      if (has(snap.channel_count) && snap.channel_count) figures.push(`${snap.channel_count} channels`);
    }
  }
  return { level, word, figures, panel: line ? 'line' : 'router', panelName: line ? 'Line' : 'Router' };
}

// The message of a failed answer: FastAPI's detail (a string, or a list of {loc, msg}), else the status.
// A validation error's place in the editor's words: the server counts hops from 0 ("hops 7" is card 8) and calls the
// role "category".
const FIELD_WORD = { category: 'role', warn_latency_ms: 'warn above', device_id: 'device' };
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
      return `${where ? `${where}: ` : ''}${roleWords(String(d.msg || '').replace(/^Value error, /, ''))}`;
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
export async function waitDiscovery(id, progress = () => {}, every = 1000) {
  for (;;) {
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
