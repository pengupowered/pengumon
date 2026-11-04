// Sign out: the confirmation step of "Sign out everywhere" (the dialog's forms post to /logout themselves).

import { registerActions } from '../../js/dispatch.js';
import { keepKeyboard } from '../../js/util.js';

function confirming(on, focus = true) {
  const ask = document.getElementById('btnSignOutEverywhere');
  document.getElementById('signOutEverywhereConfirm').hidden = !on;
  ask.setAttribute('aria-expanded', String(on));
  ask.disabled = on;
  if (focus) (on ? document.getElementById('btnSignOutEverywhereConfirm') : ask).focus();
}

const modal = document.getElementById('signOutModal');
keepKeyboard(modal, '#btnSignOutHere'); // this module loads as the dialog opens
// Closed by Esc, Close or anything else: the next opening starts fresh, without the red confirm already expanded.
new MutationObserver(() => { if (!modal.classList.contains('active')) confirming(false, false); }).observe(modal, { attributes: true, attributeFilter: ['class'] });

registerActions('click', {
  askSignOutEverywhere: () => confirming(true),
  cancelSignOutEverywhere: () => confirming(false)
});
