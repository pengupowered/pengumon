// The probe path's API (api/path.py) and discovery's (POST /api/discovery, GET /api/discovery/{id}), for the path
// editor (dialogs/path-editor) and the first-run banner on Path health. Nothing here touches the page.

// The roles a hop can have (TargetConfig.category), in the editor's plain words, nearest first.
export const ROLES = [
  ['local', 'Your network'],
  ['gateway', 'Line / gateway'],
  ['isp', 'Provider'],
  ['public', 'Internet'],
  ['dns', 'DNS server'],
  ['custom', 'Other']
];
export const ROLE_WORD = Object.fromEntries(ROLES);
export const ROLE_HINT = {
  local: 'router or LAN device', gateway: 'modem or the provider\'s gateway in your home', isp: 'a router of your provider',
  public: 'a server on the internet', dns: 'a DNS server', custom: 'not a stage of the path (a NAS, a VPN, a server)'
};

// The message of a failed answer: FastAPI's detail (a string, or a list of {loc, msg}), else the status.
export async function errorText(res) {
  const body = await res.json().catch(() => ({}));
  const detail = body.detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      const where = (d.loc || []).filter((p) => p !== 'body').join(' ');
      return `${where ? `${where}: ` : ''}${String(d.msg || '').replace(/^Value error, /, '')}`;
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
