// Drawing the page from the state: reading it from the server, the sections and the list editors, and the chrome around the
// rows (the Save label, the header meta, the problems bar, the preview panel, view only).

import { escapeHtml } from '../../js/escape.js';
import { canEdit } from '../../js/view-only.js';
import { LIST_EDITORS } from './lists/index.js';
import { loadAccess } from './access.js';
import { refreshRows } from './fields.js';
import { afterPreviewChange, hasRules, previewHtml, shown as previewShown } from './preview.js';
import { applySearch } from './search.js';
import { problemList, problemSummary } from './problems.js';
import { resetEditing } from './secrets.js';
import { aboutHtml, LIST_HEADING, sectionHtml } from './sections.js';
import { S, changeCount, hasProblems, listItems, listState, load, setList } from './store.js';
import { savedAgo } from './versions.js';
import { $, U, editors } from './ui.js';

export async function getJson(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(res.status === 503 ? 'Settings are not ready yet. Try again in a moment.' : `${url} answered ${res.status}.`);
  return res.json();
}

export async function fetchState() {
  const [schema, server] = await Promise.all([S.schema ? Promise.resolve(S.schema) : getJson('/api/settings/schema'), getJson('/api/settings'), loadAccess()]);
  load(schema, server);
}

// ---------- the page ----------
export function renderAll() {
  const body = $('settingsBody');
  const top = body.scrollTop;
  const sections = [...S.schema.sections].sort((a, b) => a.order - b.order);
  resetEditing();
  $('settingsSections').innerHTML = sections.map(sectionHtml).join('') + aboutHtml();
  const nav = [...sections.map((s) => [s.id, s.title]), ['about', 'About this install']];
  $('settingsRail').innerHTML = nav.map(([id, title]) => `<button type="button" data-action="settingsGoto" data-arg="${escapeHtml(id)}">${escapeHtml(title)}</button>`).join('');
  $('settingsJump').innerHTML = nav.map(([id, title]) => `<option value="${escapeHtml(id)}">${escapeHtml(title)}</option>`).join('');
  mountLists();
  syncAccess();
  refreshRows();
  updateChrome();
  applySearch();
  body.scrollTop = top;
}

function listContext(name) {
  return {
    schema: S.schema, listName: name, heading: LIST_HEADING[name] || 'group',
    savedIds: () => listState(name).items.map((i) => i.id),
    listIsSet: () => name in S.draft.lists ? S.draft.lists[name] !== null : listState(name).set,
    resetList: () => { setList(name, null); mountLists(); },
    markDirty: () => setList(name, editors[name].collect()),
    redraw: () => editors[name].redraw(),
    itemErrors: (listState(name).errors) || {},
    openDeviceSetup: (id) => document.querySelector(`[data-action=openDeviceSetup][data-arg="${CSS.escape(id)}"]`)?.click(),
    canEdit
  };
}

function mountLists() {
  Object.entries(LIST_EDITORS).forEach(([name, editor]) => {
    const box = document.querySelector(`[data-box="${name}"]`);
    if (!box) return;
    editors[name] = editor;
    editor.render(box, listItems(name), listContext(name));
  });
}



// ---------- the chrome around the rows: Save label, header meta, view-only ----------
export function updateChrome() {
  const n = changeCount();
  const save = $('btnSettingsSave');
  save.querySelector('span').textContent = n ? `Save ${n} change${n === 1 ? '' : 's'}` : 'Save';
  const localList = Object.values(editors).flatMap((ed) => (ed.localProblems ? ed.localProblems() : []));
  const stuck = problemList(localList);
  save.disabled = n === 0 || hasProblems() || localList.length > 0 || U.saving;
  const why = problemSummary(stuck);
  save.title = hasProblems() || localList.length ? (why || 'Fix the marked fields first') : '';
  U.problemsShown = stuck;
  const box = $('settingsProblems');
  box.hidden = !stuck.length;
  $('settingsProblemText').textContent = why;
  $('btnSettingsPreview').hidden = !hasRules();
  const panel = $('settingsPreview');
  panel.hidden = !previewShown();
  if (!panel.hidden && !panel.contains(document.activeElement)) panel.innerHTML = previewHtml();
  const saved = U.versions && U.versions[0] && U.versions[0].revision === S.server.revision ? ` · saved ${savedAgo(U.versions[0].applied_at)}` : '';
  $('settingsMeta').textContent = n && hasProblems() ? `${n} unsaved change${n === 1 ? '' : 's'} · Save is off until the marked fields are fixed` : n ? `${n} unsaved change${n === 1 ? '' : 's'}` : `revision ${S.server.revision}${saved} · nothing changes until Save`;
}

export function syncAccess() {
  const editable = canEdit();
  $('settingsFields').disabled = !editable || U.saving; // nothing typed while a Save is in flight: saved() redraws from the server's state
  $('settingsReadOnly').hidden = editable;
  $('settingsExport').hidden = !editable;
}

afterPreviewChange(() => {
  const panel = $('settingsPreview');
  panel.hidden = !previewShown();
  const days = $('settingsPreviewDays');
  const typing = days && document.activeElement === days;
  if (!typing) panel.innerHTML = previewHtml();
});
