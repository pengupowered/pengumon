// Back and reload with a changed draft ask first. The editor's history entry is the one every dialog gets (js/modal.js):
// Back with a changed draft stays and asks (`ask`, the dialog's Cancel), without one it closes the editor.

import { canEdit } from '../../js/view-only.js';
import { closeModal, onModalBack } from '../../js/modal.js';
import { $, changed } from './draft.js';

// `ask()`: what Back does with a changed draft (the dialog's Cancel, which asks).
export function installGuard(ask) {
  onModalBack('pathEditorModal', () => {
    if (changed() && canEdit()) ask(); else closeModal('pathEditorModal');
  });
  window.addEventListener('beforeunload', (ev) => {
    if ($('pathEditorModal').classList.contains('active') && changed() && canEdit()) { ev.preventDefault(); ev.returnValue = ''; }
  });
}
