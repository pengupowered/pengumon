// Settings > Sign-in: who can open this dashboard (GET /api/access), the password and the read token. Unlike the rest of
// the page these controls apply at once, without Save, as a device's password does: setting or changing the password
// signs this browser in with it (the answer sets its cookie) and every other browser out; removing it opens the
// dashboard to everyone on the network. Changing or removing a password set here asks for the current one first (the
// server checks it, and counts a wrong one as a failed sign-in). A new password is shown once, with Copy; the server
// never sends it again (a lost one is replaced). A password from PENGUMON_ACCESS_TOKEN wins over the page: the row says
// so and offers nothing. The read token (for AI assistants and scripts: it reads and calls MCP, changes nothing) is
// made, replaced and removed here too, with the current password (the environment's too), and shown once the same way;
// a new password, or removing it, removes the read token. The viewers switch below is an ordinary setting
// (access.allow_viewers), saved with Save; turning it on asks for the current password in a box under it
// (viewersConfirmHtml), which Save sends (save.js). So a browser left signed in can neither leave a read token behind
// nor open the dashboard to everyone. What is typed in a current-password box lives in that input only: kept across a
// redraw of its row (keepTyped), never in a variable or an attribute.

import { registerActions } from '../../js/dispatch.js';
import { fetchNetworkInfo, toast } from '../../js/chrome.js';
import { holdSignIn } from '../../js/session.js';
import { copyText } from '../../js/clipboard.js';
import { escapeHtml } from '../../js/escape.js';
import { icon } from '../../js/dom.js';
import { suggestPassword } from '../../js/password.js';
import { detailText } from './problems.js';
import { S, watchDraft } from './store.js';

let access = null; // GET /api/access
let ui = 'idle'; // 'idle' | 'edit' | 'confirm-remove' | 'shown'
let shown = ''; // the password while it is shown
let draft = ''; // the editor's password: a suggestion, or what the person typed (kept across redraws)
let message = '';
let tokenUi = 'idle'; // the read token: 'idle' | 'confirm-new' | 'confirm-remove' | 'shown'
let tokenShown = ''; // the read token while it is shown
let tokenMessage = '';
let busy = false;

// `keep`: what the rows show stays (a shown password, an open editor); a sign-in elsewhere on the page only refreshes it.
export async function loadAccess(keep = false) {
  const res = await fetch('/api/access', { cache: 'no-store' });
  if (!res.ok) throw new Error(res.status === 503 ? 'Settings are not ready yet. Try again in a moment.' : `/api/access answered ${res.status}.`);
  access = await res.json();
  if (!keep) forgetSecrets();
}

// Drops the password and the read token the rows have shown, what was typed into the editor and the rows' state. Called
// when the dialog opens and when it closes: a secret shown once must not stay in a module variable after that.
export function forgetSecrets() {
  ui = 'idle';
  shown = '';
  draft = '';
  message = '';
  tokenUi = 'idle';
  tokenShown = '';
  tokenMessage = '';
}

// A password set on this page: changing or removing it needs the current one.
const needsCurrent = () => access.mode === 'password' && !access.from_environment;
// Any password in force (the environment's too): the read token and letting others look need it.
const passwordInForce = () => access.mode === 'password';

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
  return 'Changes here apply at once, without Save, and need the current password. A new password signs every other browser out.';
}

function buttons() {
  if (busy || access.mode === 'locked' || access.from_environment || ui !== 'idle') return '';
  if (access.mode === 'open') return `<button type="button" class="btn sm" data-action="accessEdit" data-needs-edit>${icon('lock')}<span>Set a password</span></button>`;
  return `<button type="button" class="btn sm" data-action="accessEdit" data-needs-edit>Change</button><button type="button" class="btn sm danger" data-action="accessAskRemove" data-needs-edit>Remove</button>`;
}

// A current-password box. `data-keep`: redrawRow puts back what was typed (keepTyped); the value is never in the markup.
function currentHtml(id) {
  return `<label class="set-sub" for="${id}">Current password</label>
    <input class="input" type="password" id="${id}" autocomplete="current-password" spellcheck="false" autocapitalize="off" maxlength="128" data-keep>`;
}

function editorHtml() {
  const change = access.mode === 'password';
  return `<div class="set-access-box">${needsCurrent() ? currentHtml('setAccessCurrent') : ''}
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
  const ask = needsCurrent() ? `<div class="set-access-current">${currentHtml('setAccessRemoveCurrent')}</div>` : '';
  return `<div class="set-access-box set-access-confirm" role="alertdialog" aria-labelledby="setAccessConfirmT"><span id="setAccessConfirmT"><b>Remove the password?</b> Anyone on your network can then open the dashboard and change settings.</span>
    ${ask}<span class="set-access-b"><button type="button" class="btn sm" data-action="accessCancel" id="setAccessKeep">Keep it</button><button type="button" class="btn sm danger" data-action="accessRemove" data-needs-edit>Remove</button></span></div>`;
}

function inner() {
  const box = ui === 'edit' ? editorHtml() : ui === 'shown' ? shownHtml() : ui === 'confirm-remove' ? confirmHtml() : '';
  return `<div class="set-main"><span class="set-label" id="setAccessLabel">Who can open this dashboard</span>
      <div class="set-input set-secret-state">${stateChips()}${buttons()}</div></div>
    <div class="set-meta"><span class="set-effect">${escapeHtml(effect())}</span></div>
    ${box}<p class="set-msg" role="alert">${escapeHtml(message)}</p>`;
}

// --- the read token ---------------------------------------------------------------------------------------------------

function tokenEffect() {
  if (access.mode === 'open') return 'Needs a password first: without one, anyone on your network can read everything anyway.';
  return 'For an AI assistant (MCP) or a script, instead of the password: it reads what the dashboard shows, with client names and addresses hidden, and changes nothing. Shown once; Replace makes a new one and the old one stops working. Making, replacing or removing it needs the current password; a new password removes it.';
}

function tokenButtons() {
  if (busy || access.mode !== 'password' || tokenUi !== 'idle') return '';
  if (!access.read_token) return `<button type="button" class="btn sm" data-action="readTokenAskNew" data-needs-edit>${icon('lock')}<span>Make a read token</span></button>`;
  return `<button type="button" class="btn sm" data-action="readTokenAskNew" data-needs-edit>Replace</button><button type="button" class="btn sm danger" data-action="readTokenAskRemove" data-needs-edit>Remove</button>`;
}

const tokenCurrentHtml = () => (passwordInForce() ? `<div class="set-access-current">${currentHtml('setReadTokenCurrent')}</div>` : '');

function tokenBox() {
  if (tokenUi === 'confirm-new') {
    const replace = access.read_token;
    return `<div class="set-access-box set-access-confirm" role="alertdialog" aria-labelledby="setReadTokenConfirmT"><span id="setReadTokenConfirmT"><b>${replace ? 'Replace the read token?' : 'Make a read token?'}</b> ${replace ? 'The old one stops working at once.' : 'It is shown once.'}</span>
      ${tokenCurrentHtml()}<span class="set-access-b"><button type="button" class="btn sm" data-action="readTokenHide" id="setReadTokenKeep">Cancel</button><button type="button" class="btn sm primary" data-action="readTokenNew" data-needs-edit>${replace ? 'Replace' : 'Make it'}</button></span></div>`;
  }
  if (tokenUi === 'shown') {
    return `<div class="set-access-box"><div class="set-access-shown"><code class="mono" id="setReadTokenShown">${escapeHtml(tokenShown)}</code>
      <button type="button" class="btn sm" data-action="readTokenCopy">${icon('copy')}<span>Copy</span></button><button type="button" class="btn sm" data-action="readTokenHide">Hide</button></div>
      <p class="fine">Send it as <code>Authorization: Bearer …</code>. It is not shown again.</p></div>`;
  }
  if (tokenUi === 'confirm-remove') {
    return `<div class="set-access-box set-access-confirm" role="alertdialog" aria-labelledby="setReadTokenConfirmT"><span id="setReadTokenConfirmT"><b>Remove the read token?</b> Whatever uses it stops working.</span>
      ${tokenCurrentHtml()}<span class="set-access-b"><button type="button" class="btn sm" data-action="readTokenHide" id="setReadTokenKeep">Keep it</button><button type="button" class="btn sm danger" data-action="readTokenRemove" data-needs-edit>Remove</button></span></div>`;
  }
  return '';
}

function tokenInner() {
  const chip = access.read_token ? '<span class="chip" data-state>Made</span>' : '<span class="chip" data-state>None</span>';
  return `<div class="set-main"><span class="set-label" id="setReadTokenLabel">Read token</span>
      <div class="set-input set-secret-state">${chip}${tokenButtons()}</div></div>
    <div class="set-meta"><span class="set-effect">${escapeHtml(tokenEffect())}</span></div>
    ${tokenBox()}<p class="set-msg" role="alert">${escapeHtml(tokenMessage)}</p>`;
}

// The section's first groups (dialog.js puts them at the head of the Sign-in section).
export function accessHtml() {
  if (!access) return '';
  const token = access.mode === 'locked' ? '' : `<div class="set-group"><h4 class="set-group-t">Read token</h4><div class="set-row set-access" id="setReadToken" data-keys="read token read-only token mcp ai assistant script bearer">${tokenInner()}</div></div>`;
  return `<div class="set-group"><h4 class="set-group-t">Password</h4><div class="set-row set-access" id="setAccess" data-keys="password sign-in sign in access token">${inner()}</div></div>${token}`;
}

// What is typed in the current-password boxes of `root` ({id: value}), to put back after a redraw.
function keepTyped(root) {
  return Object.fromEntries([...root.querySelectorAll('input[data-keep]')].map((el) => [el.id, el.value]));
}

function redrawRow(id, html, focus) {
  const row = document.getElementById(id);
  if (!row) return;
  const typed = keepTyped(row);
  row.innerHTML = html;
  row.querySelectorAll('input[data-keep]').forEach((el) => { if (el.id in typed) el.value = typed[el.id]; });
  const el = focus && row.querySelector(focus);
  if (el) el.focus();
}

const redraw = (focus) => redrawRow('setAccess', inner(), focus);
const redrawToken = (focus) => redrawRow('setReadToken', tokenInner(), focus);

async function send(method, url, body) {
  const res = await holdSignIn(() => fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined }));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(detailText(data?.detail, `The server answered ${res.status}.`)); // (a 422 detail is a list)
  return data;
}

// Runs a change of the sign-in; the header's Sign out follows a password change (/api/info again). What was typed stays
// on a refusal.
// `token`: a read token change (its row shows the message); `focus()`: where focus goes in the password row afterwards.
async function act(work, done, { token = false, focus } = {}) {
  if (busy) return;
  busy = true;
  if (token) tokenMessage = ''; else message = '';
  try {
    const result = await work();
    await loadAccess(true);
    if (done) done(result);
    // the header's Sign out follows the password; a read token changes nothing there (and asking would redraw the
    // whole dialog, dropping what the token row shows)
    if (!token) fetchNetworkInfo();
  } catch (e) {
    if (token) tokenMessage = e.message; else message = e.message;
  } finally {
    busy = false;
    redraw(token ? null : focus ? focus() : '.set-access [data-action]');
    redrawToken(token ? (tokenUi === 'shown' ? '[data-action=readTokenCopy]' : '[data-action]') : null);
  }
}

function typedCurrent(id) {
  const input = document.getElementById(id);
  return input ? input.value : '';
}

// The toast after a change of the password, and what it did to the read token.
const tokenGone = (data) => (data && data.read_token_removed ? ' The read token was removed with it: make a new one.' : '');

function save() {
  const input = document.getElementById('setAccessPw');
  const password = input ? input.value : draft;
  draft = password;
  const body = { password };
  if (needsCurrent()) {
    body.current_password = typedCurrent('setAccessCurrent');
    if (!body.current_password) { message = 'Enter the current password.'; redraw('#setAccessCurrent'); return; }
  }
  if (password.trim().length < 8) { message = 'A password needs at least 8 characters.'; redraw('#setAccessPw'); return; }
  const was = access.mode;
  act(() => send('PUT', '/api/access/password', body), (data) => {
    ui = 'shown';
    shown = data.password;
    draft = '';
    toast((was === 'password' ? 'Password changed. Other browsers are signed out.' : 'Password set. This browser stays signed in.') + tokenGone(data));
  }, { focus: () => (ui === 'shown' ? '[data-action=accessCopy]' : needsCurrent() ? '#setAccessCurrent' : '#setAccessPw') });
}

function remove() {
  const body = {};
  if (needsCurrent()) {
    body.current_password = typedCurrent('setAccessRemoveCurrent');
    if (!body.current_password) { message = 'Enter the current password.'; redraw('#setAccessRemoveCurrent'); return; }
  }
  act(() => send('DELETE', '/api/access/password', body), (data) => {
    ui = 'idle';
    toast(`Password removed. Anyone on your network can open the dashboard.${tokenGone(data)}`);
  }, { focus: () => (ui === 'confirm-remove' ? '#setAccessRemoveCurrent' : '.set-access [data-action]') });
}

// The body of a read token change: the current password, or null (with the message shown) when it is missing.
function tokenBody() {
  if (!passwordInForce()) return {};
  const given = typedCurrent('setReadTokenCurrent');
  if (given) return { current_password: given };
  tokenMessage = 'Enter the current password.';
  redrawToken('#setReadTokenCurrent');
  return null;
}

function newReadToken() {
  const replacing = access.read_token;
  const body = tokenBody();
  if (!body) return;
  act(() => send('POST', '/api/access/read-token', body), (data) => {
    tokenUi = 'shown';
    tokenShown = data.read_token;
    toast(replacing ? 'Read token replaced. The old one no longer works.' : 'Read token made. Copy it now: it is not shown again.');
  }, { token: true });
}

function removeReadToken() {
  const body = tokenBody();
  if (!body) return;
  act(() => send('DELETE', '/api/access/read-token', body), () => {
    tokenUi = 'idle';
    toast('Read token removed. Whatever used it no longer gets in.');
  }, { token: true });
}

// --- letting others look: Save sends the current password with it ------------------------------------------------------

const VIEWERS = 'access.allow_viewers';
// The draft turns viewing without the password on, while a password is in force: the server wants the current one.
const viewersNeedPassword = () => !!access && passwordInForce() && !!S.draft && S.draft.values[VIEWERS] === true;

// The box under the viewers switch (sections.js puts it there); shown while the draft turns the switch on. Shown and
// hidden with data-inactive, not `hidden`, which the search box sets on every row (search.js).
export function viewersConfirmHtml() {
  return `<div class="set-row set-note set-access-current" id="setViewersConfirm" data-keys="${VIEWERS} others may look viewers view only"${viewersNeedPassword() ? '' : ' data-inactive'}>${currentHtml('setViewersCurrent')}
    <p class="fine">Turning this on needs the current password: Save sends it, and the server checks it.</p></div>`;
}

// For Save (save.js): undefined when no password is needed, else what is typed ('' when nothing is).
export function viewersPassword() {
  if (!viewersNeedPassword()) return undefined;
  return typedCurrent('setViewersCurrent');
}

export function focusViewersPassword() {
  document.querySelector('#setViewersConfirm input')?.focus();
}

watchDraft(() => {
  const box = document.getElementById('setViewersConfirm');
  if (!box) return;
  const need = viewersNeedPassword();
  box.toggleAttribute('data-inactive', !need);
  if (!need) box.querySelector('input').value = ''; // switched back off: nothing typed stays
});

async function copyShown(button, text) {
  const ok = await copyText(text);
  button.querySelector('span').textContent = ok ? 'Copied' : 'Select it and copy';
}

registerActions('click', {
  accessEdit: () => { ui = 'edit'; draft = suggestPassword(); message = ''; redraw(needsCurrent() ? '#setAccessCurrent' : '#setAccessPw'); },
  accessSuggest: () => { draft = suggestPassword(); const input = document.getElementById('setAccessPw'); if (input) { input.value = draft; input.focus(); } },
  accessCancel: () => { ui = 'idle'; shown = ''; draft = ''; message = ''; redraw('.set-access [data-action]'); },
  accessSave: () => save(),
  accessCopy: (el) => copyShown(el, shown),
  accessAskRemove: () => { ui = 'confirm-remove'; message = ''; redraw(needsCurrent() ? '#setAccessRemoveCurrent' : '#setAccessKeep'); },
  accessRemove: () => remove(),
  readTokenAskNew: () => { tokenUi = 'confirm-new'; tokenMessage = ''; redrawToken(passwordInForce() ? '#setReadTokenCurrent' : '#setReadTokenKeep'); },
  readTokenNew: () => newReadToken(),
  readTokenCopy: (el) => copyShown(el, tokenShown),
  readTokenHide: () => { tokenUi = 'idle'; tokenShown = ''; tokenMessage = ''; redrawToken('[data-action]'); },
  readTokenAskRemove: () => { tokenUi = 'confirm-remove'; tokenMessage = ''; redrawToken(passwordInForce() ? '#setReadTokenCurrent' : '#setReadTokenKeep'); },
  readTokenRemove: () => removeReadToken()
});
registerActions('input', {
  accessDraft: (el) => { draft = el.value; }
});
