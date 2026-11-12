// Settings: every setting of Pengumon on one page, and the five lists. The page shows the server's state
// (GET /api/settings, laid out by GET /api/settings/schema) and keeps a draft of changes; nothing is sent until Save
// (PUT /api/settings with the revision the page loaded; a 409 means a part you touched changed elsewhere).
//
// Opened by the header's Settings button and by `#settings` in the address. A viewer without the access token sees
// everything read-only (the fieldset is disabled and every changing control has data-needs-edit, js/view-only.js).
//
// This file opens and closes the dialog, registers its actions and keeps Back and the keyboard. The rest: ui.js the
// shared state, store.js the state and the draft, render.js and sections.js the page, validation.js the notes and the
// server's validation, save.js Save, transfer.js versions, export and import, confirm.js the question inside the dialog,
// model.js the units and range checks, fields.js one row, secrets.js the write-only slots, access.js the sign-in
// password (applied at once, not through Save), preview.js the replay of the outage rules, versions.js the kept
// versions, lists/ the list editors.

import { registerActions } from '../../js/dispatch.js';
import { closeMenus, toast } from '../../js/chrome.js';
import { browserLayout } from '../../js/layout.js';
import { closeModal, keepKeyboard, onModalBack, entryIsSpent, openModal } from '../../js/modal.js';
import { copyExport, showExport } from '../../js/export-view.js';
import { escapeHtml } from '../../js/escape.js';
import { forgetSecrets, loadAccess } from './access.js';
import { edit, choose, refreshRows, resetLayout, useZone } from './fields.js';
import { cancelPreview, defaultDays, runPreview, stopPreview } from './preview.js';
import { clearSearch, initSearch } from './search.js';
import { S, changeCount, clearDraft, setValue } from './store.js';
import { fetchVersions } from './versions.js';
import { askConfirm, confirmYes, hideConfirm, keepEditing } from './confirm.js';
import { fetchState, getJson, renderAll, syncAccess, updateChrome } from './render.js';
import { save } from './save.js';
import { importFile, loadVersion, toggleExport, toggleVersions } from './transfer.js';
import { addNote, setNotes } from './validation.js';
import { $, MODAL, U, isOpen } from './ui.js';

let opener = null; // what had focus when the dialog opened: focus goes back there when it closes

// ---------- open and close ----------
async function openSettings(sectionId, fromLink = false) {
  const mine = ++U.openSeq;
  closeMenus();
  if (!isOpen()) opener = document.activeElement;
  openModal(MODAL, { hash: '#settings', adopt: fromLink }); // one history entry, like every dialog (js/modal.js)
  clearDraft();
  U.draftSource = '';
  stopPreview();
  U.versions = null;
  U.versionsShown = false;
  $('settingsVersions').hidden = true;
  showExport('settings', false);
  setNotes([]);
  clearSearch();
  $('settingsSections').innerHTML = '<p class="fine">Loading…</p>';
  try {
    await fetchState();
    if (mine !== U.openSeq || !isOpen()) return; // closed, or opened again, while the state was on its way
  } catch (e) {
    if (mine !== U.openSeq || !isOpen()) return;
    $('settingsSections').innerHTML = '';
    addNote('crit', `<b>Settings could not be loaded.</b> ${escapeHtml(e.message)}`);
    return;
  }
  renderAll();
  $('settingsTitle').focus();
  fetchVersions().then((list) => { U.versions = list; updateChrome(); }).catch(() => {});
  if (sectionId) goto(sectionId);
}

function goto(id) {
  clearSearch();
  const target = $(`set-sec-${id}`);
  if (!target) return;
  if (target.tagName === 'DETAILS') target.open = true;
  target.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  $('settingsJump').value = id;
}

function reallyClose() {
  U.openSeq += 1;
  U.validateSeq += 1; // a validation still on its way is for a draft that is gone
  U.draftSource = '';
  stopPreview();
  $('settingsPreview').hidden = true;
  clearDraft();
  closeModal(MODAL);
  $('settingsSections').replaceChildren(); // about 5.7k nodes that nothing needs while closed; openSettings builds them again
  hideConfirm();
  forgetSecrets(); // a password or read token shown once is not kept after the dialog closes
  const back = opener && opener.isConnected && opener.offsetParent !== null ? opener : $('btnSettings');
  opener = null;
  back?.focus();
}

// Closing with a draft asks first (Cancel, the close button, Escape).
function requestClose() {
  const n = changeCount();
  if (!isOpen()) return;
  if (!n) { reallyClose(); return; }
  askConfirm(`Discard ${n} unsaved change${n === 1 ? '' : 's'}?`, 'Discard', reallyClose);
}

// ---------- actions ----------
function useBrowserLayout() {
  const mine = browserLayout();
  setValue('dashboard.order', mine.order);
  setValue('dashboard.hidden', mine.hidden);
  setValue('dashboard.colors', mine.colors);
  refreshRows();
  toast('This browser\'s layout is in the draft. Save to make new browsers start with it.');
}

function jumpToRow(key) {
  clearSearch(); // the row may have been filtered out
  const row = document.querySelector(`.set-row[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  const adv = row.closest('details');
  if (adv) adv.open = true;
  row.scrollIntoView({ block: 'center' });
  row.classList.add('flash');
  setTimeout(() => row.classList.remove('flash'), 1200);
}

// The first thing that stops Save: its row, or its list card (opened).
function firstProblem() {
  const p = U.problemsShown[0];
  if (!p) return;
  if (p.key) { jumpToRow(p.key); return; }
  clearSearch();
  const box = p.list && document.querySelector(`[data-box="${p.list}"]`);
  if (!box) return;
  const card = (p.id !== undefined ? [...box.querySelectorAll('[data-id]')].find((c) => c.dataset.id === p.id) : null) || box.querySelector(`[data-i="${p.i}"]`) || box.querySelector('.bad, .invalid') || box;
  const adv = card.closest('details.set-adv');
  if (adv) adv.open = true;
  if (card.tagName === 'DETAILS') card.open = true;
  // the card's top, then the field at fault if it is further down: centering the whole card put the top of a card taller
  // than half the dialog, and with it the problem, above the visible part
  card.scrollIntoView({ block: 'start' });
  const field = card.querySelector('[aria-invalid="true"], .bad, .set-lmsg:not(:empty), [data-lmsg]:not(:empty)');
  if (field) field.scrollIntoView({ block: 'nearest' });
  card.classList.add('flash');
  setTimeout(() => card.classList.remove('flash'), 1200);
}

function previewFromFooter() {
  const days = $('settingsPreviewDays');
  runPreview(days ? days.value : String(defaultDays()));
  $('settingsPreview').scrollIntoView({ block: 'nearest' });
}

function openSettingsAt(section) {
  closeModal('layoutModal');
  openSettings(section);
}

registerActions('click', {
  openSettings: () => openSettings(),
  openSettingsAt: (el) => openSettingsAt(el.dataset.arg),
  settingsClose: () => requestClose(),
  settingsCancel: () => requestClose(),
  settingsSave: () => save(),
  settingsReset: (el) => setValue(el.dataset.key, null),
  settingsChoice: (el) => choose(el.dataset.key, el.dataset.arg),
  settingsGoto: (el) => goto(el.dataset.arg),
  settingsJump: (el) => jumpToRow(el.dataset.arg),
  settingsFirstProblem: () => firstProblem(),
  settingsConfirmYes: () => confirmYes(),
  settingsConfirmNo: () => keepEditing(),
  settingsPreviewCancel: () => { cancelPreview(); $('settingsPreview').querySelector('[data-action=settingsPreviewRun]')?.focus(); },
  settingsVersions: () => toggleVersions(),
  settingsExport: () => toggleExport(),
  settingsExportCopy: () => copyExport('settings'),
  settingsLoadVersion: (el) => loadVersion(el.dataset.arg),
  settingsImport: () => $('settingsImportFile').click(),
  settingsUseLayout: () => useBrowserLayout(),
  settingsResetLayout: (el) => resetLayout(el),
  settingsUseZone: (el) => useZone(el.dataset.arg, el),
  settingsPreview: () => previewFromFooter(),
  settingsPreviewRun: () => runPreview($('settingsPreviewDays').value)
});
registerActions('input', { settingsEdit: (el) => edit(el) });
registerActions('change', {
  settingsEdit: (el) => edit(el),
  settingsGoto: (el) => goto(el.value),
  settingsImportFile: (el) => importFile(el)
});

document.addEventListener('editaccess', () => {
  if (!isOpen() || !S.server) return;
  syncAccess();
  // a sign-in adds what only an editor is sent (the install's paths). Every change of the sign-in (Settings > Sign-in)
  // ends here too: the field being typed in (the current password, say) keeps the focus through the redraw.
  Promise.all([getJson('/api/settings'), loadAccess(true)]).then(([server]) => {
    const focused = document.activeElement?.id;
    S.server = server;
    renderAll();
    if (focused) document.getElementById(focused)?.focus();
  }).catch(() => {});
});

// Keyboard (keepKeyboard in js/modal.js): focus moves into the dialog when it opens, Tab stays inside it and Escape closes it (asking
// first when there is a draft; with the question showing, Escape answers "keep editing").
keepKeyboard($(MODAL), '#settingsTitle', { onEscape: () => { if (!$('settingsConfirm').hidden) keepEditing(); else requestClose(); } });
initSearch();

// A link to #settings (the address typed, or set by code) opens the dialog, on the entry the link made. A Forward onto the
// entry of a Settings that was closed also lands on that address, and opens nothing.
function openFromHash() {
  if (location.hash === '#settings' && !isOpen() && !entryIsSpent()) openSettings('', true);
}
window.addEventListener('hashchange', openFromHash);
// Back with a draft: stay and ask (the entry is put back by js/modal.js); without one, close.
onModalBack(MODAL, () => {
  if (!isOpen()) return;
  if (changeCount()) requestClose(); else reallyClose();
});
// Reload, another address or closing the tab while there is a draft: the browser asks.
window.addEventListener('beforeunload', (ev) => {
  if (isOpen() && changeCount()) { ev.preventDefault(); ev.returnValue = ''; }
});
openFromHash();
