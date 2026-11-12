// How a hop is probed in the path editor: ping (the default), a TCP connection to a port, or an HTTP(S) request. The
// words and the rules (the server is authoritative: models/probes.py), the card's fields, and what an edit of them does
// to the hop. A relative import of this folder only, so the versioned static path needs no change.

import { escapeHtml } from '../../js/escape.js';

export const PROBES = [['icmp', 'Ping'], ['tcp', 'TCP connect'], ['http', 'HTTP(S) request']];
const HTTP_DEFAULTS = { http_scheme: 'https', http_path: '/', http_verb: 'HEAD' };
const PROBE_FIELDS = ['probe', 'port', 'http_scheme', 'http_path', 'http_verb'];
export const isProbeField = (field) => PROBE_FIELDS.includes(field);
const FINE = 'TLS checked · no redirects followed · no cookies or passwords sent · the answer\'s body is not read';

// A hop from an older draft or from discovery has no `probe`: it is probed by ping.
export const probeOf = (h) => (h.probe === 'tcp' || h.probe === 'http' ? h.probe : 'icmp');
const scheme = (h) => (h.http_scheme === 'http' ? 'http' : 'https');
const pathOf = (h) => (typeof h.http_path === 'string' && h.http_path !== '' ? h.http_path : '/');
const verb = (h) => (h.http_verb === 'GET' ? 'GET' : 'HEAD');
const portOf = (h) => (h.port === null || h.port === undefined || h.port === '' ? null : h.port);

// The five fields as one string, with the server's defaults: two hops that compare equal are probed the same way.
export const probeKey = (h) => (probeOf(h) === 'icmp' ? 'icmp' : [probeOf(h), portOf(h) ?? '', scheme(h), pathOf(h), verb(h)].join('|'));

// The server's probe_label: "TCP 443", "HTTPS HEAD /health", "HTTP GET :8080 /status"; '' for a ping.
export function probeLabel(h) {
  const p = probeOf(h);
  if (p === 'tcp') return `TCP ${portOf(h) ?? '?'}`;
  if (p !== 'http') return '';
  let label = `${scheme(h).toUpperCase()} ${verb(h)}`;
  const port = portOf(h);
  if (port !== null && port !== (scheme(h) === 'https' ? 443 : 80)) label += ` :${port}`;
  const path = pathOf(h);
  if (path !== '/') label += ` ${path.length > 40 ? `${path.slice(0, 40)}…` : path}`;
  return label;
}

// scheme://host[:port]path, an IPv6 host in brackets; for display only.
function probeUrl(h) {
  const host = String(h.address || '');
  const shown = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
  const port = portOf(h);
  const dflt = scheme(h) === 'https' ? 443 : 80;
  return `${scheme(h)}://${shown}${port !== null && port !== dflt ? `:${port}` : ''}${pathOf(h)}`;
}

const portBad = (h) => {
  const port = portOf(h);
  return port === null || !(Number.isInteger(Number(port)) && Number(port) >= 1 && Number(port) <= 65535);
};
const opts = (pairs, value) => pairs.map(([v, w]) => `<option value="${escapeHtml(v)}"${v === value ? ' selected' : ''}>${escapeHtml(w)}</option>`).join('');

// The card's probe fields, as flex items of its row (everything escaped). `n`: the hop's number, for the labels.
export function probeBlock(h, n) {
  const id = escapeHtml(h.id);
  const p = probeOf(h);
  const field = (name, label, control) => `<label class="field pe-probe-f"><span>${label}</span>${control}</label>`;
  const sel = (name, pairs, value, label) => `<select class="input" data-change="editPathHop" data-field="${name}" data-arg="${id}" aria-label="${label} of hop ${n}">${opts(pairs, value)}</select>`;
  const port = portOf(h);
  const input = (name, value, attrs, label) => `<input class="input${name === 'port' ? ' num' : ' mono'}" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" value="${escapeHtml(value ?? '')}" data-input="editPathHop" data-change="editPathHop" data-field="${name}" data-arg="${id}" aria-label="${label} of hop ${n}" ${attrs}>`;
  let out = field('probe', 'Probe by', sel('probe', PROBES, p, 'Probe'));
  if (p === 'tcp') {
    out += field('port', 'Port', input('port', port, `inputmode="numeric" maxlength="5" aria-invalid="${portBad(h)}"`, 'Port'));
  } else if (p === 'http') {
    out += field('http_scheme', 'Scheme', sel('http_scheme', [['https', 'HTTPS'], ['http', 'HTTP']], scheme(h), 'Scheme'));
    out += field('port', 'Port (optional)', input('port', port, `inputmode="numeric" maxlength="5" placeholder="${scheme(h) === 'https' ? 443 : 80}" aria-invalid="${port !== null && portBad(h)}"`, 'Port'));
    out += field('http_path', 'Path', input('http_path', pathOf(h), 'maxlength="255"', 'Path'));
    out += field('http_verb', 'Request', sel('http_verb', [['HEAD', 'HEAD'], ['GET', 'GET']], verb(h), 'Request'));
  }
  if (p === 'http') out += `<span class="mono pe-url" data-url="${id}">${escapeHtml(probeUrl(h))}</span><p class="fine pe-probe-fine">${escapeHtml(FINE)}</p>`;
  return out;
}

// What an edit of a probe field does to the hop. True when the card must be drawn again (the fields it shows change);
// false for typing in a text field (no re-draw: typing keeps the focus; a bad value is sent as typed and the server
// refuses it). Only the port range is checked here, the server knows the blocked ports and the path's rules.
export function editProbe(h, field, el) {
  if (field === 'probe') {
    h.probe = el.value === 'tcp' || el.value === 'http' ? el.value : 'icmp';
    h.port = null; // the fields that do not belong to the new method go back to their defaults
    Object.assign(h, HTTP_DEFAULTS);
    if (h.probe === 'http') { // an HTTP hop is always Other: its time includes the server's own work
      h.category = 'custom';
      h.group = null;
      h.joined = false;
      h.device_id = null;
    }
    return true;
  }
  if (field === 'http_scheme' || field === 'http_verb') {
    h[field] = el.value;
    return true;
  }
  if (field === 'port') {
    const text = el.value.trim();
    h.port = text === '' ? null : /^\d+$/.test(text) ? Number(text) : text;
    el.setAttribute('aria-invalid', String(portOf(h) === null ? probeOf(h) === 'tcp' : portBad(h)));
  } else if (field === 'http_path') {
    h.http_path = el.value;
  }
  const url = document.querySelector(`[data-url="${CSS.escape(h.id)}"]`);
  if (url) url.textContent = probeUrl(h);
  const chip = document.querySelector(`[data-probe-chip="${CSS.escape(h.id)}"]`); // the card's chip follows what is typed
  if (chip) chip.textContent = probeLabel(h);
  return false;
}
