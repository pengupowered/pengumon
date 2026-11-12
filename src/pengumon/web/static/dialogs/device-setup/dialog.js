// One sign-in form for every device (the device setup dialog), opened from a device card with the device's id. What
// the device reads and where it lives come from /api/devices (driver capabilities, host); the password is posted to
// /api/devices/{id}/credentials, which stores it on this host and tests it with one poll. The test reading goes to the
// device's cards at once (device-feed.js). A device whose driver says what it asks for (`sign_in`: an snmp device's
// community string, or its SNMPv3 user, authentication and privacy passwords) gets those labels, and the privacy
// password field only when it uses one. Passwords are never read back: the fields start empty.

import { registerActions } from '../../js/dispatch.js';
import { state } from '../../js/state.js';
import { closeModal, keepKeyboard, openModal } from '../../js/modal.js';
import { deviceName } from '../../js/device-text.js';
import { escapeHtml } from '../../js/escape.js';
import { icon } from '../../js/dom.js';
import { publishDevice } from '../../js/device-feed.js';

const ERRORS = { // the last reading's failure, by the driver's error kind: its title
  auth: 'Sign-in refused', unreachable: 'Device not answering', timeout: 'Device not answering', unconfigured: 'Not signed in yet',
  protocol: 'The reading made no sense', unsupported: 'Not supported by this device'
};
const READS = { access_line: 'its line signal', wan_counters: 'WAN traffic', router_system: 'CPU and memory', client_inventory: 'the connected clients' };
let device = null;
let closeTimer = 0;

const $ = (id) => document.getElementById(id);

function showResult(level, html) {
  $('setupResult').innerHTML = html ? `<div class="result st-${level}">${icon(level === 'good' ? 'ok' : level === 'crit' ? 'down' : level === 'warn' ? 'warn' : 'unknown')}<span>${html}</span></div>` : '';
}

// The typed passwords do not stay in a hidden dialog, and a pending auto-close does not outlive it.
function wipe() {
  clearTimeout(closeTimer);
  for (const id of ['setupPassword', 'setupPrivacy']) { $(id).value = ''; $(id).type = 'password'; }
}

export function openDeviceSetup(id) {
  clearTimeout(closeTimer);
  device = state.devices.find((d) => d.id === id) || null;
  if (!device || device.needs_sign_in === false) return;
  const [kind, ...rest] = String(device.label || '').split(' · '); // "<what it is> · <its name>"
  const given = rest.join(' · ');
  const name = /^[a-z]+$/.test(given) ? `the ${given}` : (given || deviceName(device)); // "modem" reads "the modem"; a model name stands alone
  $('setupTitle').textContent = `Set up ${name}`;
  $('setupMeta').textContent = rest.length ? kind : '';
  const reads = device.offers.map((c) => READS[c]).filter(Boolean);
  $('setupLead').textContent = `Pengumon signs in to ${name}'s admin page to read ${reads.length ? reads.join(', ') : 'its status'}. `
    + 'The password is kept on this computer only, in Pengumon\'s data folder, and is never sent anywhere else.';
  $('setupAddress').value = device.url || device.host || '';
  $('setupAddressHint').textContent = 'Set in Settings > Devices and services.';
  const signIn = device.sign_in || {};
  $('setupUsernameField').hidden = signIn.username === null;
  $('setupUsernameLabel').textContent = signIn.username || 'Username';
  $('setupPasswordLabel').textContent = signIn.password || 'Password';
  $('setupPassword').placeholder = signIn.password ? '' : 'Admin password';
  const privacy = !!signIn.privacy_password;
  $('setupPrivacyField').hidden = !privacy;
  $('setupPrivacyLabel').textContent = signIn.privacy_password || 'Privacy password';
  const err = $('setupError');
  err.hidden = !device.error_message;
  if (device.error_message) err.innerHTML = `${icon('lock')}<div><b>${escapeHtml(ERRORS[device.error_kind] || 'The last reading failed')}</b>${escapeHtml(device.error_message)}</div>`;
  const env = device.id.toUpperCase().replace(/-/g, '_');
  $('setupSource').textContent = privacy
    ? `The sign-in can also come from the environment (PENGUMON_DEVICE_${env}_USERNAME, PENGUMON_DEVICE_${env}_PASSWORD, PENGUMON_DEVICE_${env}_PRIVACY_PASSWORD) `
      + `or secret files (pengumon_${device.id}_password, pengumon_${device.id}_privacy_password). Both win over what is saved here, from the next start.`
    : `The sign-in can also come from the environment (PENGUMON_DEVICE_${env}_USERNAME, PENGUMON_DEVICE_${env}_PASSWORD) `
      + `or a secret file (pengumon_${device.id}_password). Both win over what is saved here, from the next start.`;
  for (const [field, button, what] of [['setupPassword', 'btnShowPassword', 'password'], ['setupPrivacy', 'btnShowPrivacy', 'privacy password']]) {
    $(field).value = '';
    $(field).type = 'password';
    $(button).innerHTML = icon('eye');
    $(button).setAttribute('aria-label', `Show ${what}`);
  }
  $('setupPrivacy').required = privacy;
  showResult('', '');
  openModal('deviceSetupModal');
  $('setupPassword').focus();
}

function togglePassword(button) {
  const input = $(button.dataset.arg === 'setupPrivacy' ? 'setupPrivacy' : 'setupPassword');
  const what = input.id === 'setupPrivacy' ? 'privacy password' : 'password';
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  button.innerHTML = icon(show ? 'eye-off' : 'eye');
  button.setAttribute('aria-label', show ? `Hide ${what}` : `Show ${what}`);
}

async function save(ev) {
  ev.preventDefault();
  if (!device) return;
  const dev = device; // the device that was tested, whatever dialog is open when the answer comes
  const result = (level, html) => { if (device === dev) showResult(level, html); }; // a late answer is not shown in another device's dialog
  const button = $('btnSaveSetup');
  const label = button.querySelector('span');
  button.disabled = true;
  label.textContent = 'Testing…';
  result('unknown', `Signing in to ${escapeHtml(dev.host || dev.id)}…`);
  try {
    const sent = { username: $('setupUsername').value.trim() || 'admin', password: $('setupPassword').value };
    if (!$('setupPrivacyField').hidden) sent.privacy_password = $('setupPrivacy').value;
    const res = await fetch(`/api/devices/${encodeURIComponent(dev.id)}/credentials`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sent)
    });
    const body = await res.json().catch(() => ({})); // a proxy's error page is not JSON
    if (res.status === 429) {
      result('warn', '<b>Too many tries.</b> Wait a minute, then try again. Nothing was changed.');
    } else if (res.ok && body.success) {
      const note = body.overridden_by ? ` Saved, but ${escapeHtml(body.overridden_by)} ranks above it: that one applies after the next start.` : '';
      const every = dev.poll_interval_seconds ? `within ${escapeHtml(String(dev.poll_interval_seconds))} s` : 'within one poll interval';
      result('good', `<b>Success: signed in and saved.</b> Readings resume ${every}.${note}`);
      $('setupPassword').value = '';
      $('setupPrivacy').value = '';
      if (body.snapshot) publishDevice(dev.id, body.snapshot);
      closeTimer = setTimeout(() => { if (device === dev) closeModal('deviceSetupModal'); }, 1500);
    } else if (!res.ok && !body.error_kind && !body.snapshot) {
      // not a sign-in result (a damaged secrets file, a proxy's page): the server's own sentence, as it is
      result('crit', typeof body.detail === 'string' ? escapeHtml(body.detail) : `<b>The test failed.</b> The server answered HTTP ${res.status}.`);
    } else {
      const why = body.snapshot && body.snapshot.error_message ? body.snapshot.error_message : (typeof body.detail === 'string' ? body.detail : 'Check the password.');
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
keepKeyboard($('deviceSetupModal'), '#setupPassword', { onClose: wipe });
