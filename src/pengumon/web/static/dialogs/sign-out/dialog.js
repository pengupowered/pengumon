// Sign out: the confirmation step of "Sign out everywhere" (the dialog's forms post to /logout themselves).

import { registerActions } from '../../js/dispatch.js';

function confirming(on) {
  const ask = document.getElementById('btnSignOutEverywhere');
  document.getElementById('signOutEverywhereConfirm').hidden = !on;
  ask.setAttribute('aria-expanded', String(on));
  ask.disabled = on;
  (on ? document.getElementById('btnSignOutEverywhereConfirm') : ask).focus();
}

registerActions('click', {
  askSignOutEverywhere: () => confirming(true),
  cancelSignOutEverywhere: () => confirming(false)
});
