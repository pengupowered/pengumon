// The notes at the top of the page, and the server's running validation of the draft (/api/settings/validate): the problems
// it finds are put on the rows, the list cards and the notes.

import { escapeHtml } from '../../js/escape.js';
import { canEdit } from '../../js/view-only.js';
import { LIST_EDITORS } from './lists/index.js';
import { refreshRows } from './fields.js';
import { refreshSlots } from './secrets.js';
import { S, changeCount, watchDraft } from './store.js';
import { updateChrome } from './render.js';
import { $, U, editors, isOpen } from './ui.js';

let validateTimer = 0;

// Notes are the answer to something the person just did (a failed Import, a loaded version, a refused Save), and they sit
// at the top of a body that may be scrolled far below: bring them into view. `quiet` is for the server's running
// validation, which updates the notes while someone types and must not move the page.
export function setNotes(list, quiet = false) {
  U.notes = list;
  const box = $('settingsNotes');
  box.innerHTML = U.notes.map((n) => `<div class="result st-${n.level}" data-note>${n.html}</div>`).join('');
  if (!quiet && U.notes.length) box.scrollIntoView({ block: 'nearest' });
}
export const addNote = (level, html) => setNotes([...U.notes, { level, html }]);

// ---------- the draft changed: refresh what depends on it, and ask the server to validate ----------
watchDraft(() => {
  if (!isOpen()) return;
  if (LIST_EDITORS.webhooks) LIST_EDITORS.webhooks.sync();
  refreshRows();
  refreshSlots();
  updateChrome();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateDraft, 400);
});

export function showServerErrors(errors, warnings) {
  S.errors = {};
  errors.forEach((e) => { if (!(e.where in S.errors)) S.errors[e.where] = e.message; });
  S.warnings = warnings || [];
  const perList = {};
  const other = [];
  Object.keys(S.errors).forEach((where) => {
    const m = /^lists\.([a-z_]+)\[(\d+)\]\.?(.*)$/.exec(where);
    if (m) (perList[m[1]] ||= {})[`${m[2]}.${m[3]}`] = S.errors[where];
    else if (!/^(values|secrets)\./.test(where)) other.push(`${escapeHtml(where)}: ${escapeHtml(S.errors[where])}`);
  });
  const unplaced = [];
  Object.entries(editors).forEach(([name, ed]) => {
    const left = ed.problems(perList[name] || {}, S.errors);
    if (Array.isArray(left)) unplaced.push(...left.map((m) => escapeHtml(typeof m === 'string' ? m : `${m.where}: ${m.message}`)));
  });
  other.push(...unplaced);
  // a warning about a list is placed at its list (the themes: where "values.display.themes" names the list's own setting)
  editors.themes?.warnings?.(S.warnings.filter((w) => w.where === 'values.display.themes').map((w) => w.message));
  refreshRows();
  refreshSlots();
  const general = [...other, ...(S.warnings.filter((w) => !/^values\./.test(w.where)).map((w) => escapeHtml(w.message)))];
  setNotes(U.notes.filter((n) => !n.validation).concat(general.map((html) => ({ level: 'warn', html, validation: true }))), true);
  updateChrome();
}

async function validateDraft() {
  const seq = ++U.validateSeq;
  if (changeCount() === 0 || !canEdit()) { showServerErrors([], []); return; }
  try {
    const res = await fetch('/api/settings/validate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: S.draft.values, lists: S.draft.lists, secrets: S.draft.secrets }) });
    if (seq !== U.validateSeq) return;
    if (!res.ok) { if (res.status !== 422 && res.status !== 400) return; }
    const body = await res.json();
    if (seq !== U.validateSeq) return;
    showServerErrors(body.errors || [], body.warnings || []);
    S.summary = body.summary || '';
  } catch (e) { /* the server decides at Save */ }
}
