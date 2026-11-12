// Checking a host name or address typed into a form.

// Why the server would refuse a host (models/hosts.validate_host), in words, or '' when it would take it.
const HOST_RE = /^[A-Za-z0-9]([A-Za-z0-9_-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9_-]*[A-Za-z0-9])?)*\.?$/;
export function hostProblem(v) {
  if (!v) return 'Enter a host name or an IP address.';
  if (/\s/.test(v) || v.startsWith('-')) return 'A host has no spaces and does not start with a dash.';
  // an IPv6 address, a link-local one with its zone (fe80::1%eth0)
  if (v.includes(':')) return /^[0-9A-Fa-f:.]+(%[A-Za-z0-9_.-]+)?$/.test(v) && (v.match(/:/g) || []).length >= 2 ? '' : 'That is not a valid IPv6 address.';
  if (/^[0-9.]+$/.test(v)) return v.split('.').length === 4 && v.split('.').every((p) => p !== '' && Number(p) <= 255 && String(Number(p)) === p) ? '' : 'That is not a valid IPv4 address.';
  return HOST_RE.test(v) && v.length <= 255 ? '' : 'That is not a valid host name.';
}

// A password Pengumon proposes for the sign-in (the first-run question, Settings > Sign-in): 16 characters in four groups,
