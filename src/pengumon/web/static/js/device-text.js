// How the server's device and client fields read as words: a device's name, a mesh node's backhaul, a client's kind.

// A device as people know it: its model from the config, else its host, else its id.
export const deviceName = (d) => (d.model ? String(d.model).replace(/_/g, ' ') : (d.host || d.id));

// A mesh node's backhaul as a few words: "Wired", or the band and signal of its Wi-Fi link ("5 GHz, -54 dBm"); '' when
// the router does not say. The fields are the server's (devices/summary.py mesh_nodes).
export function backhaulText(node) {
  if (node.backhaul === 'wired') return 'Wired';
  if (node.backhaul !== 'wifi') return '';
  const dbm = node.backhaul_rssi_dbm;
  return [node.backhaul_band || 'Wi-Fi', dbm === null || dbm === undefined ? null : `${dbm} dBm`].filter(Boolean).join(', ');
}

// The client `kind` words the server sends (api/clients: phone, computer, ...) as a label and a sprite icon. A kind not
// listed here, or none, is shown as before.
export const CLIENT_KINDS = {
  phone: ['Phone', 'phone'],
  computer: ['Computer', 'host'],
  game: ['Game console', 'host'],
  network: ['Network device', 'router'],
  printer: ['Printer', 'host']
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
