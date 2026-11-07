// Settings > Sign-in: who can open this dashboard (GET /api/access) and the password. Unlike the rest of the page these
// controls apply at once, without Save, as a device's password does: setting or changing the password signs this browser
// in with it (the answer sets its cookie) and every other browser out; removing it opens the dashboard to everyone on
// the network. A new password is shown once, with Copy; the server never sends it again (a lost one is replaced). A
// password from PENGUMON_ACCESS_TOKEN wins over the page: the row says so and offers nothing. The viewers switch below
// is an ordinary setting (access.allow_viewers), saved with Save.

import { registerActions } from '../../js/dispatch.js';
import { fetchNetworkInfo, toast } from '../../js/chrome.js';
import { holdSignIn } from '../../js/session.js';
import { copyText, escapeHtml, icon, suggestPassword } from '../../js/util.js';

let access = null; // GET /api/access
let ui = 'idle'; // 'idle' | 'edit' | 'confirm-remove' | 'shown'
let shown = ''; // the password while it is shown
let draft = ''; // the editor's password: a suggestion, or what the person typed (kept across redraws)
let message = '';
let busy = false;

// `keep`: what the row shows stays (a shown password, an open editor); a sign-in elsewhere on the page only refreshes it.
export async function loadAccess(keep = false) {
  const res = await fetch('/api/access', { cache: 'no-store' });
  if (!res.ok) throw new Error(res.status === 503 ? 'Settings are not ready yet. Try again in a moment.' : `/api/access answered ${res.status}.`);
  access = await res.json();
  if (keep) return;
  ui = 'idle';
  shown = '';
  draft = '';
  message = '';
}

function stateChips() {
  if (access.mode === 'open') return '<span class="chip" data-state>Anyone on your network</span>';
  if (access.mode === 'locked') return '<span class="chip st st-crit" data-state>Nobody: the secrets file cannot be read</span>';
  const env = access.from_environment ? ' <span class="chip st st-unknown" data-badge="env">From environment · PENGUMON_ACCESS_TOKEN</span>' : '';
  return `<span class="chip" data-state>Only people with the password</span>${env}`;
}

function effect() {
  if (access.mode === 'open') return 'No password: anyone on your network can open the dashboard and change settings. A password applies at once, without Save; this browser stays signed in.';
  if (access.mode === 'locked') return 'The password is kept in the secrets file (secrets.json), which is damaged or missing. Fix or restore the file, or set PENGUMON_ACCESS_TOKEN, then restart Pengumon.';
  if (access.from_environment) return 'Set where Pengumon is started, and it wins over this page. To change or remove it, change that variable and restart Pengumon.';
  return 'Changes here apply at once, without Save. A new password signs every other browser out.';
}

function buttons() {
  if (busy || access.mode === 'locked' || access.from_environment || ui !== 'idle') return '';
  if (access.mode === 'open') return `<button type="button" class="btn sm" data-action="accessEdit" data-needs-edit>${icon('lock')}<span>Set a password</span></button>`;
  return `<button type="button" class="btn sm" data-action="accessEdit" data-needs-edit>Change</button><button type="button" class="btn sm danger" data-action="accessAskRemove" data-needs-edit>Remove</button>`;
}

function editorHtml() {
  const change = access.mode === 'password';
  return `<div class="set-access-box">
    <label class="set-sub" for="setAccessPw">${change ? 'New password' : 'Password'}</label>
    <div class="set-secret-edit"><input class="input mono" type="text" id="setAccessPw" autocomplete="new-password" spellcheck="false" autocapitalize="off" maxlength="128" value="${escapeHtml(draft)}" data-input="accessDraft" aria-describedby="setAccessPwHint">
      <button type="button" class="btn sm" data-action="accessSuggest">${icon('refresh')}<span>New one</span></button></div>
    <p class="fine" id="setAccessPwHint">At least 8 characters. Keep this one or type your own.${change ? ' Every other browser is signed out and needs the new one.' : ' Anyone without it then gets the sign-in page.'}</p>
    <div class="set-access-b"><button type="button" class="btn sm" data-action="accessCancel">Cancel</button><button type="button" class="btn sm primary" data-action="accessSave" data-needs-edit>${icon('ok')}<span>Save password</span></button></div></div>`;
}

function shownHtml() {
  return `<div class="set-access-box"><div class="set-access-shown"><code class="mono" id="setAccessShown">${escapeHtml(shown)}</code>
    <button type="button" class="btn sm" data-action="accessCopy">${icon('copy')}<span>Copy</span></button><button type="button" class="btn sm" data-action="accessCancel">Hide</button></div></div>`;
}

function confirmHtml() {
  return `<div class="set-access-box set-access-confirm" role="alertdialog" aria-labelledby="setAccessConfirmT"><span id="setAccessConfirmT"><b>Remove the password?</b> Anyone on your network can then open the dashboard and change settings.</span>
    <span class="set-access-b"><button type="button" class="btn sm" data-action="accessCancel" id="setAccessKeep">Keep it</button><button type="button" class="btn sm danger" data-action="accessRemove" data-needs-edit>Remove</button></span></div>`;
}

function inner() {
  const box = ui === 'edit' ? editorHtml() : ui === 'shown' ? shownHtml() : ui === 'confirm-remove' ? confirmHtml() : '';
  return `<div class="set-main"><span class="set-label" id="setAccessLabel">Who can open this dashboard</span>
      <div class="set-input set-secret-state">${stateChips()}${buttons()}</div></div>
    <div class="set-meta"><span class="set-effect">${escapeHtml(effect())}</span></div>
    ${box}<p class="set-msg" role="alert">${escapeHtml(message)}</p>`;
}

// The section's first group (dialog.js puts it at the head of the Sign-in section).
export function accessHtml() {
  if (!access) return '';
  return `<div class="set-group"><h4 class="set-group-t">Password</h4><div class="set-row set-access" id="setAccess" data-keys="password sign-in sign in access token">${inner()}</div></div>`;
}

function redraw(focus) {
  const row = document.getElementById('setAccess');
  if (!row) return;
  row.innerHTML = inner();
  const el = focus && row.querySelector(focus);
  if (el) el.focus();
}

async function send(method, url, body) {
  const res = await holdSignIn(() => fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined }));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `The server answered ${res.status}.`);
  return data;
}

// Runs a change of the sign-in; the header's Sign out follows it (/api/info again).
async function act(work, done) {
  if (busy) return;
  busy = true;
  message = '';
  try {
    const result = await work();
    await loadAccess();
    if (done) done(result);
    fetchNetworkInfo();
  } catch (e) {
    message = e.message;
  } finally {
    busy = false;
    redraw(ui === 'shown' ? '[data-action=accessCopy]' : '.set-access [data-action]');
  }
}

function save() {
  const input = document.getElementById('setAccessPw');
  const password = input ? input.value : draft;
  draft = password;
  if (password.trim().length < 8) { message = 'A password needs at least 8 characters.'; redraw('#setAccessPw'); return; }
  const was = access.mode;
  act(() => send('PUT', '/api/access/password', { password }), (data) => {
    ui = 'shown';
    shown = data.password;
    toast(was === 'password' ? 'Password changed. Other browsers are signed out.' : 'Password set. This browser stays signed in.');
  });
}

async function copyShown(button) {
  const ok = await copyText(shown);
  button.querySelector('span').textContent = ok ? 'Copied' : 'Select it and copy';
}

registerActions('click', {
  accessEdit: () => { ui = 'edit'; draft = suggestPassword(); message = ''; redraw('#setAccessPw'); },
  accessSuggest: () => { draft = suggestPassword(); const input = document.getElementById('setAccessPw'); if (input) { input.value = draft; input.focus(); } },
  accessCancel: () => { ui = 'idle'; shown = ''; draft = ''; message = ''; redraw('.set-access [data-action]'); },
  accessSave: () => save(),
  accessCopy: (el) => copyShown(el),
  accessAskRemove: () => { ui = 'confirm-remove'; redraw('#setAccessKeep'); },
  accessRemove: () => act(() => send('DELETE', '/api/access/password'), () => toast('Password removed. Anyone on your network can open the dashboard.'))
});
registerActions('input', { accessDraft: (el) => { draft = el.value; } });
