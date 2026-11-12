// The question inside the dialog ("Discard 3 unsaved changes?", "Replace the draft with ..."), and the rule that loading
// something over a draft asks first.

import { confirmReplaceDraft } from '../../js/export-view.js';
import { changeCount } from './store.js';
import { $, MODAL } from './ui.js';

let confirmed = null; // what "Discard" / "Replace" does
let returnFocus = null; // what had focus when the question came up

export function askConfirm(text, yesLabel, fn) {
  confirmed = fn;
  $('settingsConfirmText').textContent = text;
  $('btnConfirmYes').textContent = yesLabel;
  if ($('settingsConfirm').hidden) returnFocus = document.activeElement;
  $('settingsConfirm').hidden = false;
  $('btnConfirmNo').focus();
}
export function hideConfirm() { confirmed = null; $('settingsConfirm').hidden = true; }

// "Keep editing" (and Escape on the question): back to what had focus, else the Cancel button, so the keyboard goes on.
export function keepEditing() {
  const usable = returnFocus && $(MODAL).contains(returnFocus) && !returnFocus.disabled && returnFocus.offsetParent !== null;
  const back = usable ? returnFocus : $('btnSettingsCancel');
  returnFocus = null;
  hideConfirm();
  back.focus();
}

export function confirmYes() {
  const fn = confirmed;
  hideConfirm();
  if (fn) fn();
}

// Replacing the draft (a version, an imported file) asks when there is one.
export function replaceDraft(text, fn) {
  confirmReplaceDraft(changeCount() > 0, text, fn, askConfirm);
}
