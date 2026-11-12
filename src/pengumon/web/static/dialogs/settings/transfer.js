// The kept versions (load one as a draft), Export (the YAML) and Import (a file read into a draft).

import { escapeHtml } from '../../js/escape.js';
import { exportToggle, reveal } from '../../js/export-view.js';
import { renderAll } from './render.js';
import { addNote, setNotes } from './validation.js';
import { replaceDraft } from './confirm.js';
import { detailText } from './problems.js';
import { S, applyDocument, changeCount } from './store.js';
import { fetchVersion, fetchVersions, versionsHtml } from './versions.js';
import { $, U } from './ui.js';

export function renderVersions() {
  const box = $('settingsVersions');
  box.hidden = !U.versionsShown;
  if (U.versionsShown && U.versions) box.innerHTML = versionsHtml(U.versions, S.server.revision);
}

export async function toggleVersions() {
  U.versionsShown = !U.versionsShown;
  $('btnSettingsVersions').setAttribute('aria-expanded', String(U.versionsShown));
  if (U.versionsShown && !U.versions) {
    $('settingsVersions').hidden = false;
    $('settingsVersions').innerHTML = '<p class="fine">Loading…</p>';
    reveal($('settingsVersions'));
    try { U.versions = await fetchVersions(); } catch (e) { $('settingsVersions').innerHTML = `<p class="fine">${escapeHtml(e.message)}</p>`; return; }
  }
  renderVersions();
  if (U.versionsShown) reveal($('settingsVersions'));
}

// Export shows the YAML here, with Copy and a download link (exportToggle in js/export-view.js).
export const toggleExport = () => exportToggle('settings', { url: '/api/settings/export', what: 'settings', intro: 'Every setting and list as YAML, without secrets. Import reads it in another install.', file: 'pengumon-settings.yaml' });

export async function loadVersion(n) {
  try {
    const doc = await fetchVersion(n);
    replaceDraft(`Replace the draft with revision ${n}?`, () => {
      applyDocument(doc);
      U.draftSource = 'restore';
      renderAll();
      setNotes([{ level: 'unknown', html: changeCount() ? `<b>Revision ${n} is loaded as a draft.</b> Review it, then Save to restore it.` : `<b>Revision ${n} is the same as the settings now.</b> There is nothing to restore.` }]);
    });
  } catch (e) {
    addNote('crit', escapeHtml(e.message));
  }
}

async function importText(name, text) {
  try {
    const res = await fetch('/api/settings/import', { method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: text });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(detailText(data.detail, `The server answered ${res.status}.`));
    applyDocument(data);
    U.draftSource = 'yaml_import';
    setNotes([{ level: 'unknown', html: `<b>Read ${escapeHtml(name)}.</b> It is a draft: review it, then Save.` }, ...(data.notes || []).map((t) => ({ level: 'warn', html: escapeHtml(t) }))]);
    renderAll();
  } catch (e) {
    addNote('crit', `<b>The file was not imported.</b> ${escapeHtml(e.message)}`);
  }
}

export async function importFile(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  const text = await file.text();
  replaceDraft(`Replace the draft with ${file.name}?`, () => importText(file.name, text));
}
