// The path editor: the probe path as a draft of hop cards in path order (GET /api/path), edited here and applied in
// one PUT /api/path; nothing goes live until Apply. Opened by Path health's Edit button, by its first-run banner and its
// "Your path changed" notice (with a discovery's draft, data-arg "discovery:<id>") and by Rediscover inside it.
//
// At the top, the draft as Path health draws it: one node per hop (or per merged run of hops), each with what the
// prober measures now; a click on a node goes to its card, and Merge / Split on the links make two neighbouring hops of
// one role one node. Every card shows its live measurement from the `targets` topic (the same figures as Path health:
// the median of the last 60 probes and the loss over them), updated while the editor is open; a hop that is not probed
// yet says so, with Test (one probe) and Preview (30 s of probes, POST /api/path/preview) to measure it before Apply.
// Whether a hop ignores ping is observed by the server (engine/path_rules.py), never set here. A hop of your network or
// the line can be the configured device whose readings Pengumon shows: linked automatically when one device is at its
// address (GET /api/path's device_matches, engine/device_links.py), changed by hand. A removed hop stops being probed;
// its history is kept (hidden) unless "delete its history" is ticked. A draft from discovery shows each hop's
// confidence, its reasons (evidence), a suggested device driver and the difference from the live path. Earlier
// applied paths (GET /api/path/versions) load as a draft to restore.

import { bus } from '../../js/bus.js';
import { registerActions } from '../../js/dispatch.js';
import { errorText, fetchPath, ROLE_EFFECT, startDiscovery, waitDiscovery } from '../../js/path-api.js';
import { settingsValues, val } from '../../js/settings-values.js';
import { canEdit, closeModal, confirmReplaceDraft, copyExport, escapeHtml, keepKeyboard, openModal, showExport } from '../../js/util.js';
import { $, changed, edited, hopById, note, payload, pe, startDraft } from './draft.js';
import { addHop, cancelAddress, cleanNames, connect, edit, focusHop, merge, move, pickDevice, remove, replaceAddress, startAddress, test, toggleChooser, undo } from './edits.js';
import { installGuard, releaseGuard } from './guard.js';
import { runPreview, stopRunningPreview } from './preview.js';
import { onTargets, render, unwatchDevices } from './render.js';
import { loadVersions, toggleExport, versionDraft } from './versions.js';

// ---------- the draft ----------
// Makes `hops` the draft and draws it.
function load(path, hops, source, options) {
  startDraft(path, hops, source, options);
  pe.leaveArmed = false;
  pe.pendingReplace = null;
  stopRunningPreview();
  render();
}

// Loading an earlier path or a file replaces the draft: with edits in it that are not applied, ask first (as Settings
// does, util.confirmReplaceDraft), in the note line, with the question's two answers.
function replaceDraft(text, replace) {
  confirmReplaceDraft(edited(), text, replace, (question, yes, go) => {
    pe.pendingReplace = go;
    note('warn', `<b>${escapeHtml(question)}</b> Your changes to the draft are not applied. `
      + `<button type="button" class="btn sm danger" data-action="replacePathDraft">${escapeHtml(yes)}</button> `
      + '<button type="button" class="btn sm" id="btnKeepPathDraft" data-action="keepPathDraft">Keep editing</button>');
    $('btnKeepPathDraft').focus();
  });
}

function keepDraft() {
  pe.pendingReplace = null;
  note('', '');
  const back = pe.draft && document.querySelector('#pathEditorHops .pe-name');
  if (back) back.focus({ preventScroll: true });
}

function replaceNow() {
  const go = pe.pendingReplace;
  pe.pendingReplace = null;
  note('', '');
  if (go) go();
}

// ---------- opening and closing ----------
async function open(arg = '') {
  note('', '');
  pe.opener = document.activeElement instanceof HTMLElement && !$('pathEditorModal').contains(document.activeElement) ? document.activeElement : pe.opener;
  $('pathDiscAsn').checked = !!val(settingsValues(), 'discovery.asn_lookup', false); // as Settings says, off unless the owner turned it on
  showExport('path', false);
  openModal('pathEditorModal');
  if (!pe.unsubscribe) pe.unsubscribe = bus.subscribe('targets', onTargets);
  $('pathEditorChain').innerHTML = '';
  $('pathEditorHops').innerHTML = '<li class="skel-stack" aria-busy="true"><div class="skel line" style="width:90%"></div><div class="skel line" style="width:70%"></div></li>';
  try {
    const path = await fetchPath();
    if (arg.startsWith('discovery:')) {
      const res = await fetch(`/api/discovery/${encodeURIComponent(arg.slice(10))}`);
      if (!res.ok) throw new Error(await errorText(res));
      showDiscovery(path, await res.json());
    } else {
      load(path, path.hops, 'live', { matches: path.device_matches || {} });
    }
  } catch (e) {
    note('crit', `<b>The path could not be loaded.</b> ${escapeHtml(e.message)}`);
  }
  const first = $('pathEditorHops').querySelector('.pe-name');
  if (first && document.activeElement && document.activeElement.classList.contains('m-close')) first.focus({ preventScroll: true });
  loadVersions();
}

function closed() {
  releaseGuard();
  pe.pendingReplace = null;
  if (pe.opener && pe.opener.isConnected) pe.opener.focus({ preventScroll: true });
  pe.opener = null;
  if (pe.unsubscribe) { pe.unsubscribe(); pe.unsubscribe = null; }
  unwatchDevices();
  stopRunningPreview();
  pe.draft = null;
  pe.removed = [];
}

function showDiscovery(path, run) {
  if (run.state !== 'done' || !run.draft) {
    note('crit', `<b>Discovery did not finish.</b> ${escapeHtml(run.progress || '')} ${(run.notes || []).map(escapeHtml).join(' ')}`);
    if (!pe.draft) load(path, path.hops, 'live', { matches: path.device_matches || {} });
    return;
  }
  load(path, run.draft.hops || [], 'discovery', { notes: run.notes || [], matches: path.device_matches || {} });
  const counts = ['new', 'changed', 'gone'].map((s) => [s, pe.draft.hops.filter((h) => h.status === s).length]).filter(([, c]) => c);
  const diff = counts.length ? ` Compared with the live path: ${counts.map(([s, c]) => `${c} ${s === 'gone' ? 'not found' : s}`).join(', ')}.` : '';
  const notes = (run.notes || []).length ? `<ul class="pe-notes">${run.notes.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>` : '';
  const found = pe.draft.hops.filter((h) => h.status !== 'gone').length; // the live hops discovery did not see are kept, not found
  note('unknown', `<b>Discovery found ${found} hop${found === 1 ? '' : 's'}.</b> Check each one's role and reasons, then Apply.${diff}${notes}`);
}

async function rediscover() {
  if (!pe.draft || !canEdit()) return;
  const button = $('btnRediscover');
  button.disabled = true;
  note('unknown', 'Starting discovery…');
  try {
    const id = await startDiscovery({ asnLookup: $('pathDiscAsn').checked, snmp: $('pathDiscSnmp').checked });
    const run = await waitDiscovery(id, (text) => note('unknown', `${escapeHtml(text)}`));
    showDiscovery(await fetchPath(), run);
  } catch (e) {
    note(e.unavailable ? 'warn' : 'crit', `<b>Discovery did not run.</b> ${escapeHtml(e.message)}`);
  } finally {
    button.disabled = !canEdit();
  }
}

// The Router or Line panel, where the device's details are: the editor closes (a changed draft asks first).
function showDevicePanel(el) {
  if (pe.draft && JSON.stringify(payload()) !== pe.draft.start && !pe.leaveArmed) {
    pe.leaveArmed = true;
    note('warn', '<b>Your changes are not applied.</b> Apply them first, or press the link again to leave them and see the panel.');
    return;
  }
  pe.leaveArmed = false;
  const panel = el.dataset.arg === 'line' ? 'line' : 'router';
  closeModal('pathEditorModal');
  const tab = document.querySelector(`[data-action=selectTab][data-arg="${panel === 'line' ? 'line' : 'network'}"]`);
  if (tab && getComputedStyle(tab).display !== 'none' && tab.offsetParent !== null) tab.click();
  setTimeout(() => {
    const target = document.querySelector(`[data-panel="${panel}"] .panel`) || document.querySelector(`[data-panel="${panel}"]`); // the card: its section box may be display: contents
    if (target) target.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }, 50);
}

async function apply() {
  if (!pe.draft || pe.applying || !canEdit()) return;
  pe.applying = true;
  const button = $('btnApplyPath');
  const label = button.querySelector('span');
  button.disabled = true;
  label.textContent = 'Applying…';
  try {
    const res = await fetch('/api/path', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_revision: pe.draft.base, hops: payload() }) });
    if (res.status === 409) {
      note('crit', '<b>The path was changed elsewhere</b> (another tab or another device) after this editor opened, so nothing was applied. '
        + '<button type="button" class="btn sm outline" data-action="reloadPathEditor">Load the current path</button>');
      return;
    }
    if (!res.ok) { note('crit', `<b>Nothing was applied.</b> ${escapeHtml(await errorText(res))}`); return; }
    stopRunningPreview();
    const failed = [];
    for (const r of pe.removed.filter((x) => x.live && x.deleteHistory)) {
      const del = await fetch(`/api/path/history/${encodeURIComponent(r.hop.id)}`, { method: 'DELETE' });
      if (!del.ok) failed.push(r.hop.name);
    }
    if (failed.length) { note('warn', `<b>Applied</b>, but the history of ${escapeHtml(failed.join(', '))} could not be deleted.`); return; }
    pe.draft.opened = JSON.stringify(payload()); // applied: nothing left to lose, so the reload below does not ask
    note('good', '<b>Applied.</b> The new path is probed from now on; the dashboard reloads to show it.');
    setTimeout(() => location.reload(), 600); // every panel then draws the new path from the start
  } catch (e) {
    note('crit', `<b>Nothing was applied.</b> ${escapeHtml(e.message)}`);
  } finally {
    pe.applying = false;
    button.disabled = !canEdit();
    label.textContent = 'Apply';
  }
}

function loadVersion(el) {
  if (!canEdit()) return;
  const n = el.dataset.arg;
  replaceDraft(`Replace the draft with revision ${n}?`, async () => {
    try {
      const { path, hops, version } = await versionDraft(n);
      load(path, hops, 'version', { revision: version.revision, matches: path.device_matches || {} });
      note('unknown', `<b>Revision ${escapeHtml(String(version.revision))} is in the editor as a draft.</b> Review it, then Apply to restore it; a hop that comes back keeps its history.`);
      $('pathEditorChain').scrollIntoView({ block: 'nearest' });
    } catch (e) {
      note('crit', `<b>That path could not be loaded.</b> ${escapeHtml(e.message)}`);
    }
  });
}

function importFile(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  replaceDraft(`Replace the draft with ${file.name}?`, () => importText(file));
}

async function importText(file) {
  try {
    const res = await fetch('/api/path/import', { method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: await file.text() });
    if (!res.ok) throw new Error(await errorText(res));
    const { hops, device_matches: matches } = await res.json();
    const path = await fetchPath();
    load(path, hops, 'import', { matches: matches || {} });
    note('unknown', `<b>Read ${hops.length} hop${hops.length === 1 ? '' : 's'} from ${escapeHtml(file.name)}.</b> Review them, then Apply.`);
  } catch (e) {
    note('crit', `<b>The file was not imported.</b> ${escapeHtml(e.message)}`);
  }
}

// Every way out (Cancel, X, Esc) comes here: a changed draft is not thrown away without a question.
export function cancel(force = false) {
  if (!force && changed() && canEdit()) {
    note('warn', '<b>Your changes are not applied.</b> Closing now throws them away. '
      + '<button type="button" class="btn sm" data-action="discardPathEditor">Discard changes and close</button>');
    return;
  }
  closeModal('pathEditorModal'); // the observer below lets go of the live data and stops a preview
}

installGuard(() => cancel());

registerActions('click', {
  openPathEditor: (el) => open(el.dataset.arg || ''),
  cancelPathEditor: () => cancel(),
  discardPathEditor: () => cancel(true),
  replacePathDraft: () => replaceNow(),
  keepPathDraft: () => keepDraft(),
  reloadPathEditor: () => open(''),
  movePathHop: (el) => move(el),
  mergePathHops: (el) => merge(el, true),
  splitPathHops: (el) => merge(el, false),
  focusPathHop: (el) => focusHop(el),
  removePathHop: (el) => remove(el),
  undoRemovePathHop: (el) => undo(el),
  testPathHop: (el) => test(el),
  connectPathDevice: (el) => connect(el),
  changePathDevice: (el) => toggleChooser(el),
  editPathAddress: (el) => startAddress(el),
  cancelPathAddress: (el) => cancelAddress(el.dataset.arg),
  cleanPathNames: () => cleanNames(),
  showDevicePanel: (el) => showDevicePanel(el),
  loadPathVersion: (el) => loadVersion(el),
  previewPath: () => runPreview(),
  applyPath: () => apply(),
  rediscoverPath: () => rediscover(),
  pathExport: () => toggleExport(),
  pathExportCopy: () => copyExport('path'),
  importPath: () => $('pathImportFile').click()
});
registerActions('input', { editPathHop: (el, ev) => edit(el, ev) });
registerActions('change', {
  editPathHop: (el, ev) => edit(el, ev),
  pickPathDevice: (el) => pickDevice(el),
  pathAddRoleChanged: (el) => { $('pathAddRoleFx').textContent = ROLE_EFFECT[el.value] || ''; },
  togglePathHistory: (el) => { const r = pe.removed.find((x) => x.hop.id === el.dataset.arg); if (r) r.deleteHistory = el.checked; },
  importPathFile: (el) => importFile(el)
});
registerActions('submit', {
  addPathHop: (el, ev) => addHop(ev),
  replacePathAddress: (el, ev) => replaceAddress(el, ev)
});

// Keyboard (util.keepKeyboard): focus moves into the dialog when it opens, Tab stays inside it and Escape closes it (or
// leaves the address being replaced, or the question about replacing the draft). Closing, by any way, lets go of the live data and
// stops a running preview.

// which hop cards have "More" open survives a re-draw
$('pathEditorHops').addEventListener('toggle', (ev) => {
  const d = ev.target;
  if (!(d instanceof Element) || !d.classList.contains('pe-more')) return;
  const h = pe.draft && hopById(d.dataset.more);
  if (h) h.moreOpen = d.open;
}, true);
keepKeyboard($('pathEditorModal'), '.pe-name', {
  onEscape: () => { if (pe.pendingReplace) keepDraft(); else if (pe.addressing) cancelAddress(pe.addressing); else cancel(); },
  onClose: closed
});
