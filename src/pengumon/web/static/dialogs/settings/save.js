// Save: PUT /api/settings with the draft, and what follows (the notes, the page redrawn from the server's state, the
// settings values and themes refreshed) or, on a 409, the parts that changed elsewhere.

import { bus } from '../../js/bus.js';
import { toast, fetchNetworkInfo } from '../../js/chrome.js';
import { escapeHtml } from '../../js/escape.js';
import { refreshSettingsValues } from '../../js/settings-values.js';
import { refreshThemes } from '../../js/themes.js';
import { conflictNames, detailText } from './problems.js';
import { S, changed, clearDraft, dropConflicts, hasProblems, changeCount } from './store.js';
import { stopPreview } from './preview.js';
import { focusViewersPassword, viewersPassword } from './access.js';
import { fetchVersions } from './versions.js';
import { fetchState, renderAll, syncAccess, updateChrome } from './render.js';
import { addNote, setNotes, showServerErrors } from './validation.js';
import { renderVersions } from './transfer.js';
import { $, U, isOpen } from './ui.js';

const sentence = (names) => names.map((n) => (S.spec[n] ? `${S.spec[n].group ? `${S.spec[n].group}: ` : ''}${S.spec[n].label}` : n)).join(', ');

export async function save() {
  if (U.saving || !changeCount() || hasProblems()) return;
  // A field that still has the focus has its `change` pending; the browser fires it when the fieldset below is disabled, i.e.
  // after the answer is in. Fire it now, so what is typed goes into the Save.
  const typing = document.activeElement;
  if (typing && typing !== document.body && $('settingsFields').contains(typing)) typing.blur();
  if (!changeCount() || hasProblems()) return;
  // turning on "Others may look" while a password is in force: the current password goes with it (access.js)
  const current = viewersPassword();
  if (current === '') {
    addNote('crit', '<b>Not saved.</b> Enter the current password under “Others may look”: turning it on needs it.');
    focusViewersPassword();
    return;
  }
  U.saving = true;
  updateChrome();
  syncAccess();
  const body = { base_revision: S.server.revision, values: S.draft.values, lists: S.draft.lists, secrets: S.draft.secrets };
  if (current) body.current_password = current;
  try {
    const res = await fetch(`/api/settings${U.draftSource ? `?source=${U.draftSource}` : ''}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (res.ok) await saved(data);
    else if (res.status === 409) await conflicted(data);
    else if (res.status === 400 || res.status === 422) {
      showServerErrors(data.errors || [], []);
      addNote('crit', `<b>Not saved.</b> ${escapeHtml(detailText(data.detail, 'The server refused the change.'))}`);
    } else addNote('crit', `<b>Not saved.</b> ${escapeHtml(detailText(data.detail, `The server answered ${res.status}.`))}`);
  } catch (e) {
    addNote('crit', '<b>Not saved.</b> The server could not be reached.');
  } finally {
    U.saving = false;
    updateChrome();
    syncAccess();
    if (document.activeElement === document.body && isOpen()) $('settingsTitle').focus(); // the field that had the focus was disabled
  }
}

async function saved(result) {
  const applied = result.applied || [];
  toast(result.summary || 'Settings saved');
  fetchNetworkInfo(); // the page's time zone may have changed
  const after = [];
  if ((result.live_failed || []).length) after.push({ level: 'warn', html: `<b>Saved, but not applied yet:</b> ${escapeHtml(sentence(result.live_failed))}. It applies at the next start.` });
  if ((result.restart_needed || []).length) after.push({ level: 'warn', html: `<b>Applies after a restart:</b> ${escapeHtml(sentence(result.restart_needed))}.` });
  clearDraft();
  U.validateSeq += 1; // an answer for the draft that was just saved would show its problems against nothing
  U.draftSource = '';
  stopPreview();
  try { await fetchState(); } catch (e) { after.push({ level: 'warn', html: 'Saved. The page could not re-read the settings; reopen it.' }); }
  setNotes(after);
  if (S.server) { renderAll(); fetchVersions().then((list) => { U.versions = list; renderVersions(); updateChrome(); }).catch(() => {}); }
  refreshSettingsValues().then(() => bus.publish('settings:saved', applied)); // (the Incidents card regroups on it)
  refreshThemes(); // the default theme, the effects switch or a custom theme may have changed
  if (applied.includes('display.grafana_url')) grafanaLinks();
  if (applied.includes('devices')) setTimeout(() => location.reload(), 1500);
}

// The header's Grafana menu is drawn by the server into the page; after a Save of the Grafana address, refill it from the
// server's current links (shown or hidden with them) so the new address works without a reload.
async function grafanaLinks() {
  const box = document.getElementById('tbGrafana');
  const menu = document.getElementById('grafanaMenu');
  if (!box || !menu) return;
  try {
    const res = await fetch('/api/settings/grafana-links', { cache: 'no-store' });
    if (!res.ok) return;
    const links = await res.json();
    menu.innerHTML = links.map((l) => `<a role="menuitem" href="${escapeHtml(l.url)}" title="${escapeHtml(l.title)}" target="_blank" rel="noopener noreferrer"><svg class="i" aria-hidden="true"><use href="#i-chart"/></svg>${escapeHtml(l.label)}<svg class="i sm" aria-hidden="true"><use href="#i-external"/></svg></a>`).join('');
    box.hidden = !links.length;
    document.getElementById('headerMenu')?.replaceChildren(); // the phone menu copies the links when it opens
  } catch (e) { /* the links follow at the next load */ }
}

async function conflicted(data) {
  const conflicts = data.conflicts || [];
  try { await fetchState(); } catch (e) { addNote('crit', '<b>Changed elsewhere,</b> and the page could not re-read the settings.'); return; }
  const named = conflictNames(conflicts);
  dropConflicts(conflicts);
  setNotes([{ level: 'warn', html: `<b>Changed elsewhere; kept theirs:</b> ${escapeHtml(named.join('; '))}. Your other changes are still here: press Save again.` }]);
  renderAll();
  changed();
}
