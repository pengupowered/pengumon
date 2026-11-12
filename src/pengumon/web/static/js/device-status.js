// What a device snapshot says, as words and view-model parts: its status on the card, the error block, the one-line summary.
// Pure functions of the snapshot; nothing here touches the page.

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

// A device in one line, from its latest snapshot (the live summary or GET /api/devices/{id}), for the places that name a
// device without its card (the editor's device line, a Path health node's tooltip): {level, word, figures, panel,
// panelName}. The word is the card's ("Healthy" for a router, "Line up" for a line, statusOf above), the
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
