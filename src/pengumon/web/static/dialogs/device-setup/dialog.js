// One sign-in form for every device (the device setup dialog), opened from a device card with the device's id. What
// the device reads and where it lives come from /api/devices (driver capabilities, host); the password is posted to
// /api/devices/{id}/credentials, which stores it on this host and tests it with one poll. The test reading goes to the
// device's cards at once (device-feed.js).

import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { closeModal, deviceName, escapeHtml, icon, keepKeyboard, openModal } from '../../js/util.js';
import { publishDevice } from '../../js/device-feed.js';

const ERRORS = { // the last reading's failure, by the driver's error kind: its title
  auth: 'Sign-in refused', unreachable: 'Device not answering', timeout: 'Device not answering', unconfigured: 'Not signed in yet',
  protocol: 'The reading made no sense', unsupported: 'Not supported by this device'
};
const READS = { access_line: 'its line signal', wan_counters: 'WAN traffic', router_system: 'CPU and memory', client_inventory: 'the connected clients' };
let device = null;

const $ = (id) => document.getElementById(id);

function result(level, html) {
  $('setupResult').innerHTML = html ? `<div class="result st-${level}">${icon(level === 'good' ? 'ok' : level === 'crit' ? 'down' : level === 'warn' ? 'warn' : 'unknown')}<span>${html}</span></div>` : '';
}

export function openDeviceSetup(id) {
  device = state.devices.find((d) => d.id === id) || null;
  if (!device) return;
  const name = deviceName(device);
  $('setupTitle').textContent = `Set up ${name}`;
  $('setupMeta').textContent = device.driver || '';
  const reads = device.offers.map((c) => READS[c]).filter(Boolean);
  $('setupLead').textContent = `pengumon signs in to ${name}'s admin page to read ${reads.length ? reads.join(', ') : 'its status'}. `
    + 'The password is stored on this host only, in local storage that is not in git, and is never sent anywhere else.';
  $('setupAddress').value = device.url || device.host || '';
  $('setupAddressHint').textContent = `From Settings > Devices (${device.id})`;
  const err = $('setupError');
  err.hidden = !device.error_message;
  if (device.error_message) err.innerHTML = `${icon('lock')}<div><b>${escapeHtml(ERRORS[device.error_kind] || 'The last reading failed')}</b>${escapeHtml(device.error_message)}</div>`;
  const env = device.id.toUpperCase().replace(/-/g, '_');
  $('setupSource').textContent = `Sign-in source, in order: the environment (PENGUMON_DEVICE_${env}_USERNAME, PENGUMON_DEVICE_${env}_PASSWORD), `
    + `a secret file (pengumon_${device.id}_password), then what is saved here. The first two win after the next start.`;
  const pw = $('setupPassword');
  pw.value = '';
  pw.type = 'password';
  $('btnShowPassword').innerHTML = icon('eye');
  $('btnShowPassword').setAttribute('aria-label', 'Show password');
  result('', '');
  openModal('deviceSetupModal');
  $('setupPassword').focus();
}

function togglePassword(button) {
  const input = $('setupPassword');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  button.innerHTML = icon(show ? 'eye-off' : 'eye');
  button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
}

async function save(ev) {
  ev.preventDefault();
  if (!device) return;
  const button = $('btnSaveSetup');
  const label = button.querySelector('span');
  button.disabled = true;
  label.textContent = 'Testing…';
  result('unknown', `Signing in to ${escapeHtml(device.host || device.id)}…`);
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(device.id)}/credentials`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('setupUsername').value.trim() || 'admin', password: $('setupPassword').value })
    });
    const body = await res.json();
    if (res.ok && body.success) {
      const note = body.overridden_by ? ` Saved, but ${escapeHtml(body.overridden_by)} ranks above it: that one applies after the next start.` : '';
      const every = device.poll_interval_seconds ? `within ${device.poll_interval_seconds} s` : 'within one poll interval';
      result('good', `<b>Success: signed in and saved.</b> Readings resume ${every}.${note}`);
      $('setupPassword').value = '';
      if (body.snapshot) publishDevice(device.id, body.snapshot);
      setTimeout(() => closeModal('deviceSetupModal'), 1500);
    } else {
      const why = body.snapshot && body.snapshot.error_message ? body.snapshot.error_message : (body.detail || 'Check the password.');
      if (body.error_kind === 'unreachable' || body.error_kind === 'timeout') {
        result('warn', `<b>Device unreachable.</b> ${escapeHtml(why)} The password was stored; it is tried again at the next poll.`);
      } else if (body.error_kind === 'auth' || !body.error_kind) {
        result('crit', `<b>Sign-in refused.</b> ${escapeHtml(why)} Nothing was changed on the device.`);
      } else {
        result('crit', `<b>The test failed.</b> ${escapeHtml(why)}`);
      }
    }
  } catch (e) {
    result('crit', `<b>The test failed:</b> ${escapeHtml(e.message)}`);
  } finally {
    button.disabled = false;
    label.textContent = 'Test and save';
  }
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  openDeviceSetup: (el) => openDeviceSetup(el.dataset.arg),
  toggleSetupPassword: (el) => togglePassword(el)
});
registerActions('submit', {
  saveDeviceCredentials: (el, ev) => save(ev)
});
keepKeyboard($('deviceSetupModal'), '#setupPassword');
