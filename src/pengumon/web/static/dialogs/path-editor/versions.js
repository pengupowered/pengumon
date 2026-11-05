// Earlier applied paths (GET /api/path/versions) and Export (the saved path as YAML) in the path editor.

import { fetchPath, fetchVersion, fetchVersions } from '../../js/path-api.js';
import { escapeHtml, exportToggle, icon } from '../../js/util.js';
import { $, pe } from './draft.js';
import { permissions } from './render.js';

// ---------- earlier paths ----------
const when = (s) => new Date(s * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });

export async function loadVersions() {
  const box = $('pathVersions');
  let versions;
  try { versions = await fetchVersions(); } catch (_) { versions = null; }
  if (!Array.isArray(versions) || !pe.draft) { box.hidden = true; return; }
  const current = pe.draft.base;
  const older = versions.filter((v) => v.revision !== current);
  box.hidden = !older.length;
  if (!older.length) return;
  const previous = older[0];
  const row = (v) => `<li><span class="pe-ver-t"><b>Revision ${escapeHtml(String(v.revision))}</b> <span class="muted">${escapeHtml(when(v.applied_at))} · ${escapeHtml(String(v.hop_count))} hop${v.hop_count === 1 ? '' : 's'}</span><span class="pe-ver-s">${escapeHtml(v.summary || '')}</span></span>`
    + `<button type="button" class="btn sm" data-action="loadPathVersion" data-needs-edit data-arg="${escapeHtml(String(v.revision))}">Load</button></li>`;
  $('pathVersionList').innerHTML = `<button type="button" class="btn sm outline" data-action="loadPathVersion" data-needs-edit data-arg="${escapeHtml(String(previous.revision))}">${icon('history')}Restore the previous path</button>`
    + `<ul class="pe-gone pe-vers">${older.slice(0, 50).map(row).join('')}</ul>`;
  permissions();
}

// An earlier path as a draft's parts: {path (the live one), hops (the version's, positioned), version}.
export async function versionDraft(revision) {
  const [path, version] = await Promise.all([fetchPath(), fetchVersion(revision)]);
  return { path, hops: (version.hops || []).map((h, i) => ({ position: i, ...h })), version };
}

// ---------- export ----------
// The saved path as YAML, shown with Copy and a download link (util.exportToggle).
export const toggleExport = () => exportToggle('path', { url: '/api/path/export', what: 'path', intro: 'The probe path as YAML. Import reads it in another install.', file: 'pengumon-path.yaml' });
