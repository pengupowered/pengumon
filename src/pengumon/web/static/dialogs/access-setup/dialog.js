// The first-run question (POST /api/access/setup): who can open this dashboard. It loads and opens by itself while
// /api/info says `access_setup` (no password from anywhere, and nobody answered yet), for whoever opens the dashboard
// first; any other visit never loads it. Two
// choices: anyone on the home network (preselected: one click) or only people with a password (Pengumon proposes one;
// "others may look" is optional). A password signs this browser in at once (the answer sets its cookie) and is shown
// here once, with Copy. There is no close button and Escape does nothing: the question is answered, not skipped.

import { registerActions } from '../../js/dispatch.js';
import { fetchNetworkInfo, toast } from '../../js/chrome.js';
import { holdSignIn } from '../../js/session.js';
import { state } from '../../js/state.js';
import { closeModal, copyText, keepKeyboard, openModal, suggestPassword } from '../../js/util.js';

const MODAL = 'accessSetupModal';
const $ = (id) => document.getElementById(id);
let done = false; // the password is set and shown: the button closes the dialog
let busy = false;

const choice = () => (document.querySelector('input[name=accessChoice]:checked') || {}).value || 'open';
const showError = (text) => { $('accessSetupError').textContent = text; };

function showChoice() {
  const password = choice() === 'password';
  $('accessSetupPassword').hidden = !password;
  if (password && !$('accessSetupPw').value) $('accessSetupPw').value = suggestPassword();
  showError('');
}

function open() {
  keepKeyboard($(MODAL), '#accessChoiceOpen', { onEscape: () => {} });
  openModal(MODAL);
}

function finishOpen() {
  closeModal(MODAL);
  toast('Anyone on your home network can open the dashboard. Settings > Sign-in changes it.');
}

function showDone(password) {
  done = true;
  $('accessSetupAsk').hidden = true;
  $('accessSetupDone').hidden = false;
  $('accessSetupShown').textContent = password;
  $('accessSetupWhere').textContent = `On another device, open ${location.origin} and sign in with it. Copy it now: it is not shown again (Settings > Sign-in can set a new one).`;
  $('btnAccessSetup').querySelector('span').textContent = 'Done';
  $('btnAccessSetupCopy').focus();
  fetchNetworkInfo(); // the header's Sign out button
}

async function submit(ev) {
  ev.preventDefault();
  if (done) { closeModal(MODAL); return; }
  if (busy) return;
  const body = { choice: choice() };
  if (body.choice === 'password') {
    body.password = $('accessSetupPw').value;
    body.viewers = $('accessSetupViewers').checked;
    if (body.password.trim().length < 8) { showError('A password needs at least 8 characters.'); $('accessSetupPw').focus(); return; }
  }
  busy = true;
  $('btnAccessSetup').disabled = true;
  try {
    const res = await holdSignIn(() => fetch('/api/access/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
    const data = await res.json().catch(() => ({}));
    if (res.status === 409) showError('Someone else answered this a moment ago. Reload the page to see what they chose.');
    else if (!res.ok) showError(data.detail || `The server answered ${res.status}. Try again.`);
    else if (data.mode === 'open') finishOpen();
    else {
      showDone(data.password);
      if (data.viewers_saved === false) showError('The password is set, but "Others may look" could not be saved. Turn it on in Settings > Sign-in.');
    }
  } catch (e) {
    showError('The server could not be reached. Try again.');
  } finally {
    busy = false;
    $('btnAccessSetup').disabled = false;
  }
}

async function copyPassword() {
  const ok = await copyText($('accessSetupShown').textContent);
  $('btnAccessSetupCopy').querySelector('span').textContent = ok ? 'Copied' : 'Select it and copy';
}

registerActions('click', {
  accessSetupSuggest: () => { $('accessSetupPw').value = suggestPassword(); $('accessSetupPw').focus(); },
  accessSetupCopy: () => copyPassword()
});
registerActions('change', { accessSetupChoice: () => showChoice() });
registerActions('submit', { accessSetupSubmit: (el, ev) => submit(ev) });

// Loaded lazily, when the page's /api/info answer says `access_setup` (dialog.json "info", js/panels/index.js): open.
if (state.networkInfo && state.networkInfo.access_setup) open();
