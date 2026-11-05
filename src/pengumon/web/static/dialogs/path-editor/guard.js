// Back and reload with a changed draft ask first (the path editor's history guard). The first changed draft gets a history
// entry of its own (the entry before it is the page without the hash); closing the editor takes that entry away again, so
// no dead Back press stays.

import { canEdit, closeModal } from '../../js/util.js';
import { $, changed, pe } from './draft.js';

const pageToken = Math.random().toString(36).slice(2); // an entry of an earlier page load (a reload keeps history.state) is not this page's
const guardState = () => !!(history.state && history.state.pathEditor === pageToken);

export function guardBack() {
  if (pe.backGuard || !changed() || !canEdit()) return;
  history.pushState({ pathEditor: pageToken }, '');
  pe.backGuard = true;
}

// The editor closed: its history entry goes away.
export function releaseGuard() {
  if (pe.backGuard) { pe.backGuard = false; if (guardState()) history.back(); }
}

// `ask()`: what Back does with a changed draft (the dialog's Cancel, which asks).
export function installGuard(ask) {
  window.addEventListener('popstate', () => {
    if (!pe.backGuard || guardState() || !$('pathEditorModal').classList.contains('active')) return;
    if (changed() && canEdit()) { // Back pressed: stay (the entry is put back) and ask
      history.pushState({ pathEditor: pageToken }, '');
      ask();
    } else { pe.backGuard = false; closeModal('pathEditorModal'); }
  });
  // A reload (Apply does one) keeps the history entry of the editor's draft: the new page takes it away, so no Back press is dead.
  window.addEventListener('load', () => { if (history.state && history.state.pathEditor) history.back(); }, { once: true });
  window.addEventListener('beforeunload', (ev) => {
    if ($('pathEditorModal').classList.contains('active') && changed() && canEdit()) { ev.preventDefault(); ev.returnValue = ''; }
  });
}
