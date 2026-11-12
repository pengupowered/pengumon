// Write-only secret slots (ntfy topic and token, a webhook's URL, a header's value). The server never sends a value, only
// {set, source, host}; the page can replace a slot (a password box whose text goes into the draft) or remove it. After a
// Save the box is empty again and the row shows "Set · host".

import { registerActions } from '../../js/dispatch.js';
import { escapeHtml } from '../../js/escape.js';
import { S, setSecret } from './store.js';

const editing = new Set(); // slots whose password box is open

export const slotState = (slot) => (S.server && S.server.secrets[slot]) || { set: false };

export function resetEditing() { editing.clear(); }

// The slot's current state in words: "Set · ntfy.example.com", "Not set", "From environment · VAR", or the pending change.
function stateText(slot, spec) {
  const st = slotState(slot);
  if (slot in S.draft.secrets) return S.draft.secrets[slot] === '' ? 'Will be removed (unsaved)' : 'New value entered (unsaved)';
  if (st.source === 'env') return `From environment${spec && spec.env ? ` · ${spec.env}` : ''}`;
  if (!st.set) return 'Not set';
  return `Set${st.host ? ` · ${st.host}` : ''}`;
}

// The inside of a slot box. `spec` is the schema's secret entry (label, kind, env) or a small stand-in for a header.
function slotInner(slot, spec) {
  const st = slotState(slot);
  const env = st.source === 'env' || st.source === 'secret_file';
  const pending = slot in S.draft.secrets;
  const label = escapeHtml(spec.label);
  const slotAttr = escapeHtml(slot);
  const open = editing.has(slot) && !env && !pending; // the password box is up: it has its own Cancel
  const buttons = env || open ? '' : pending
    ? `<button type="button" class="btn sm" data-action="settingsSecretUndo" data-slot="${slotAttr}" data-needs-edit>Undo</button>`
    : `<button type="button" class="btn sm" data-action="settingsSecretReplace" data-slot="${slotAttr}" data-needs-edit>${st.set ? 'Replace' : 'Set'}</button>`
      + (st.set ? `<button type="button" class="btn sm danger" data-action="settingsSecretRemove" data-slot="${slotAttr}" data-needs-edit>Remove</button>` : '');
  const box = open
    ? `<div class="set-secret-edit"><input class="input mono" type="password" autocomplete="new-password" spellcheck="false" data-input="settingsSecretInput" data-slot="${slotAttr}" aria-label="New value of ${label}"${spec.kind === 'url' ? ' placeholder="https://…"' : ''}>
        <button type="button" class="btn sm" data-action="settingsSecretCancel" data-slot="${slotAttr}">Cancel</button></div>` : '';
  return `<div class="set-main"><span class="set-label">${label}</span>
      <div class="set-input set-secret-state"><span class="chip${env ? ' st st-unknown' : ''}" data-state>${escapeHtml(stateText(slot, spec))}</span>${buttons}</div></div>
    ${box}<p class="set-msg" data-msg role="alert">${escapeHtml(S.errors[`secrets.${slot}`] || '')}</p>`;
}

export function slotHtml(slot, spec) {
  return `<div class="set-row set-slot" data-slot="${escapeHtml(slot)}">${slotInner(slot, spec)}</div>`;
}

// Specs the boxes were built with, by slot (the dialog registers them as it renders).
const specs = new Map();
export function rememberSlot(slot, spec) { specs.set(slot, spec); }

function redraw(slot) {
  document.querySelectorAll(`.set-slot[data-slot="${CSS.escape(slot)}"]`).forEach((box) => {
    box.innerHTML = slotInner(slot, specs.get(slot) || { label: slot, kind: 'token' });
    const input = box.querySelector('input');
    if (input) input.focus();
  });
}

export function refreshSlots() {
  document.querySelectorAll('.set-slot[data-slot]').forEach((box) => {
    const slot = box.dataset.slot;
    if (box.querySelector('input') && document.activeElement === box.querySelector('input')) return; // being typed in
    box.innerHTML = slotInner(slot, specs.get(slot) || { label: slot, kind: 'token' });
  });
}

registerActions('click', {
  settingsSecretReplace: (el) => openBox(el.dataset.slot),
  settingsSecretCancel: (el) => dropPending(el.dataset.slot),
  settingsSecretRemove: (el) => removeSlot(el.dataset.slot),
  settingsSecretUndo: (el) => dropPending(el.dataset.slot)
});

function openBox(slot) {
  editing.add(slot);
  redraw(slot);
}

function dropPending(slot) {
  editing.delete(slot);
  setSecret(slot, undefined);
  redraw(slot);
}

function removeSlot(slot) {
  editing.delete(slot);
  setSecret(slot, '');
  redraw(slot);
}

// Typing in a slot's box: the text goes into the draft (kept in memory only; it is not shown anywhere else).
registerActions('input', { settingsSecretInput: (el) => secretInput(el) });

function secretInput(el) {
  const slot = el.dataset.slot;
  const text = el.value;
  if (text === '') setSecret(slot, undefined); else setSecret(slot, text);
  const state = el.closest('.set-slot').querySelector('[data-state]');
  if (state) state.textContent = text === '' ? stateText(slot, specs.get(slot)) : 'New value entered (unsaved)';
}
