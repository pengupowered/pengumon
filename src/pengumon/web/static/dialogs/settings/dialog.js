// Settings: every setting of Pengumon on one page, and the four lists. The page shows the server's state
// (GET /api/settings, laid out by GET /api/settings/schema) and keeps a draft of changes; nothing is sent until Save
// (PUT /api/settings with the revision the page loaded; a 409 means a part you touched changed elsewhere).
//
// Opened by the header's Settings button and by `#settings` in the address. A viewer without the access token sees
// everything read-only (the fieldset is disabled and every changing control has data-needs-edit, util.js).
//
// store.js holds the state and the draft, model.js the units and range checks, fields.js one row, secrets.js the
// write-only slots, preview.js the replay of the outage rules, versions.js the kept versions, lists/ the list editors.

import { registerActions } from '../../js/dispatch.js';
import { browserLayout } from '../../js/layout.js';
import { closeMenus, toast } from '../../js/chrome.js';
import { refreshSettingsValues } from '../../js/settings-values.js';
import { canEdit, closeModal, copyExport, escapeHtml, exportViewHtml, openModal } from '../../js/util.js';
import { LIST_EDITORS } from './lists/index.js';
import { edit, choose, refreshRows, rowHtml } from './fields.js';
import { cancelPreview, defaultDays, hasRules, afterPreviewChange, previewHtml, runPreview, shown as previewShown, stopPreview } from './preview.js';
import { rememberSlot, refreshSlots, resetEditing, slotHtml } from './secrets.js';
import {
  S, applyDocument, changeCount, changed, clearDraft, dropConflicts, hasProblems, listItems, listState, load, watchDraft, setList, setValue
} from './store.js';
import { fetchVersion, fetchVersions, savedAgo, versionsHtml } from './versions.js';

const $ = (id) => document.getElementById(id);
const MODAL = 'settingsModal';
const isOpen = () => $(MODAL).classList.contains('active');

let saving = false;
let draftSource = ''; // '' | 'restore' (a loaded version) | 'yaml_import' (an imported file): sent as ?source= on Save
let versions = null; // GET /api/settings/versions, once read
let versionsShown = false;
let exportShown = false;
let confirmed = null; // what "Discard" / "Replace" does
let validateTimer = 0;
let validateSeq = 0;
let notes = []; // [{level, html}] at the top of the page
let ownBack = false; // reallyClose took the guard entry away with history.back(): the hashchange that follows is not the user's
let guarded = false; // a history entry was added for this draft, so Back comes back to the dialog (guardBack)
let opener = null; // what had focus when the dialog opened: focus goes back there when it closes
let returnFocus = null; // what had focus when the Discard question came up
const editors = {}; // list name -> the editor of lists/, bound to its container while rendered

// ---------- Back with a draft ----------
// The first change of a draft gives the dialog a history entry of its own (the entry before it is the page without
// #settings), so Back lands on the hashchange below, which asks instead of leaving the page with the draft. Without a
// draft Back leaves as it always did: nothing is lost. A stale extra entry is left behind only in a session with a draft.
function guardBack() {
  const url = `${location.pathname}${location.search}`;
  history.replaceState(null, '', url);
  history.pushState(null, '', `${url}#settings`);
  guarded = true;
}

// ---------- loading ----------
async function getJson(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(res.status === 503 ? 'Settings are not ready yet. Try again in a moment.' : `${url} answered ${res.status}.`);
  return res.json();
}

async function fetchState() {
  const [schema, server] = await Promise.all([S.schema ? Promise.resolve(S.schema) : getJson('/api/settings/schema'), getJson('/api/settings')]);
  load(schema, server);
}

// ---------- notes at the top ----------
// Notes are the answer to something the person just did (a failed Import, a loaded version, a refused Save), and they sit
// at the top of a body that may be scrolled far below: bring them into view. `quiet` is for the server's running
// validation, which updates the notes while someone types and must not move the page.
function setNotes(list, quiet = false) {
  notes = list;
  const box = $('settingsNotes');
  box.innerHTML = notes.map((n) => `<div class="result st-${n.level}" data-note>${n.html}</div>`).join('');
  if (!quiet && notes.length) box.scrollIntoView({ block: 'nearest' });
}
const addNote = (level, html) => setNotes([...notes, { level, html }]);

// ---------- layout of the page ----------
const bySetting = (a, b) => a.order - b.order;

// [{name, specs}] groups of a section in page order; a setting with no group heads the section.
function groupsOf(section) {
  const specs = S.schema.settings.filter((s) => s.section === section.id || (s.also_in || []).includes(section.id)).sort(bySetting);
  const groups = [];
  specs.forEach((s) => {
    let g = groups.find((x) => x.name === (s.group || ''));
    if (!g) { g = { name: s.group || '', specs: [] }; groups.push(g); }
    g.specs.push(s);
  });
  return groups;
}

function listHtml(name) {
  const box = `<div class="set-list-box" data-box="${name}"></div>`;
  // the devices editor (lists/devices.js) draws only its cards; the other editors draw their own heading (lists/kit.js)
  return name === 'devices' ? `<div class="set-list"><div class="set-list-h"><h4>Devices</h4></div>${box}</div>` : box;
}

function secretsHtml(section) {
  return S.schema.secrets.filter((s) => s.section === section && !s.pattern).map((s) => { rememberSlot(s.slot, s); return slotHtml(s.slot, s); }).join('');
}

function groupHtml(section, group) {
  const rows = group.specs.map((s) => rowHtml(s, section.id)).join('');
  let extra = '';
  if (section.id === 'alerts' && group.name === 'Channels') extra = secretsHtml('alerts') + listHtml('webhooks');
  if (section.id === 'devices' && group.name === 'Host app checks') extra = listHtml('services');
  if (section.id === 'display' && !group.name) {
    extra = `<div class="set-row" data-extra="layout"><div class="set-main"><span class="set-label">Panel layout</span>
      <div class="set-input"><button type="button" class="btn sm outline" data-action="settingsUseLayout" data-needs-edit>Use this browser's layout for everyone</button></div></div>
      <div class="set-meta">Writes this browser's panel order and hidden panels into the two settings above; Save applies them as the default for every browser that has not arranged its own.</div></div>`;
  }
  return `<div class="set-group">${group.name ? `<h4 class="set-group-t">${escapeHtml(group.name)}</h4>` : ''}${rows}${extra}</div>`;
}

function sectionHtml(section) {
  let groups = groupsOf(section);
  let head = '';
  let tail = '';
  if (section.id === 'devices') head = listHtml('devices');
  if (section.id === 'probing') tail = listHtml('dns_probes');
  if (section.id === 'alerts' && !groups.some((g) => g.name === 'Channels')) groups = [{ name: 'Channels', specs: [] }, ...groups];
  if (section.id === 'devices' && !groups.some((g) => g.name === 'Host app checks')) groups.push({ name: 'Host app checks', specs: [] });
  const body = `${head}${groups.map((g) => groupHtml(section, g)).join('')}${tail}`;
  if (section.id === 'advanced') {
    return `<details class="set-adv" id="set-sec-advanced" data-section="advanced"><summary><h3 class="section-t">${escapeHtml(section.title)}</h3><span class="muted">Change these only if you know why.</span></summary>${body}</details>`;
  }
  return `<section class="set-sec" id="set-sec-${section.id}" data-section="${section.id}" aria-labelledby="set-sec-${section.id}-t"><h3 class="section-t" id="set-sec-${section.id}-t">${escapeHtml(section.title)}</h3>${body}</section>`;
}

function aboutHtml() {
  const a = S.server.about;
  if (!a) {
    return `<section class="set-sec" id="set-sec-about" data-section="about"><h3 class="section-t">About this install</h3><p class="fine top">What stays outside this page (the listen address, the data folder, the config file, the access token) is shown to people signed in.</p></section>`;
  }
  const rows = [
    ['Version', a.version], ['Listens on', a.listen], ['Data folder', a.data_dir], ['Config file', a.config_path], ['Database', a.db_path || '—'],
    ['Access token', a.token_set ? 'set' : 'not set'], ['Viewing without the token', a.view_without_token ? 'allowed' : 'not allowed'], ['Metrics export', a.otlp_host || 'off']
  ];
  const cfg = a.config_yaml && a.config_yaml.differing_keys ? `<p class="fine">config.yaml is ignored after the first import; ${a.config_yaml.differing_keys} value${a.config_yaml.differing_keys === 1 ? '' : 's'} in it differ from this page.</p>` : '';
  const report = (a.import_report || []).length ? `<details><summary class="fine">The first import left ${a.import_report.length} thing${a.import_report.length === 1 ? '' : 's'} out</summary><ul class="fine">${a.import_report.map((r) => `<li>${escapeHtml(r.key)}: ${escapeHtml(r.reason)}</li>`).join('')}</ul></details>` : '';
  return `<section class="set-sec" id="set-sec-about" data-section="about"><h3 class="section-t">About this install</h3>
    <dl class="set-about">${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('')}</dl>${cfg}${report}</section>`;
}

function renderAll() {
  const body = $('settingsBody');
  const top = body.scrollTop;
  const sections = [...S.schema.sections].sort((a, b) => a.order - b.order);
  resetEditing();
  $('settingsSections').innerHTML = sections.map(sectionHtml).join('') + aboutHtml();
  const nav = [...sections.map((s) => [s.id, s.title]), ['about', 'About this install']];
  $('settingsRail').innerHTML = nav.map(([id, title]) => `<button type="button" data-action="settingsGoto" data-arg="${id}">${escapeHtml(title)}</button>`).join('');
  $('settingsJump').innerHTML = nav.map(([id, title]) => `<option value="${id}">${escapeHtml(title)}</option>`).join('');
  mountLists();
  syncAccess();
  refreshRows();
  updateChrome();
  body.scrollTop = top;
}

function listContext(name) {
  return {
    schema: S.schema, listName: name,
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
function updateChrome() {
  const n = changeCount();
  const save = $('btnSettingsSave');
  save.querySelector('span').textContent = n ? `Save ${n} change${n === 1 ? '' : 's'}` : 'Save';
  const local = Object.values(editors).some((ed) => ed.localProblems && ed.localProblems().length);
  save.disabled = n === 0 || hasProblems() || local || saving;
  save.title = hasProblems() ? 'Fix the marked fields first' : '';
  $('btnSettingsPreview').hidden = !hasRules();
  const panel = $('settingsPreview');
  panel.hidden = !previewShown();
  if (!panel.hidden && !panel.contains(document.activeElement)) panel.innerHTML = previewHtml();
  const saved = versions && versions[0] && versions[0].revision === S.server.revision ? ` · saved ${savedAgo(versions[0].applied_at)}` : '';
  $('settingsMeta').textContent = n && hasProblems() ? `${n} unsaved change${n === 1 ? '' : 's'} · Save is off until the marked fields are fixed` : n ? `${n} unsaved change${n === 1 ? '' : 's'}` : `revision ${S.server.revision}${saved} · nothing changes until Save`;
}

function syncAccess() {
  const editable = canEdit();
  $('settingsFields').disabled = !editable;
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

// ---------- the draft changed: refresh what depends on it, and ask the server to validate ----------
watchDraft(() => {
  if (!isOpen()) return;
  if (changeCount() && !guarded) guardBack();
  if (LIST_EDITORS.webhooks) LIST_EDITORS.webhooks.sync();
  refreshRows();
  refreshSlots();
  updateChrome();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateDraft, 400);
});

function showServerErrors(errors, warnings) {
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
  refreshRows();
  refreshSlots();
  const general = [...other, ...(S.warnings.filter((w) => !/^values\./.test(w.where)).map((w) => escapeHtml(w.message)))];
  setNotes(notes.filter((n) => !n.validation).concat(general.map((html) => ({ level: 'warn', html, validation: true }))), true);
  updateChrome();
}

async function validateDraft() {
  const seq = ++validateSeq;
  if (changeCount() === 0 || !canEdit()) { showServerErrors([], []); return; }
  try {
    const res = await fetch('/api/settings/validate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: S.draft.values, lists: S.draft.lists, secrets: S.draft.secrets }) });
    if (seq !== validateSeq) return;
    if (!res.ok) { if (res.status !== 422 && res.status !== 400) return; }
    const body = await res.json();
    if (seq !== validateSeq) return;
    showServerErrors(body.errors || [], body.warnings || []);
    S.summary = body.summary || '';
  } catch (e) { /* the server decides at Save */ }
}

// ---------- open and close ----------
async function openSettings(sectionId) {
  closeMenus();
  if (!isOpen()) opener = document.activeElement;
  openModal(MODAL);
  history.replaceState(null, '', `${location.pathname}${location.search}#settings`);
  guarded = false;
  clearDraft();
  draftSource = '';
  stopPreview();
  versions = null;
  versionsShown = false;
  $('settingsVersions').hidden = true;
  showExport(false);
  setNotes([]);
  $('settingsSections').innerHTML = '<p class="fine">Loading…</p>';
  try {
    await fetchState();
  } catch (e) {
    $('settingsSections').innerHTML = '';
    addNote('crit', `<b>Settings could not be loaded.</b> ${escapeHtml(e.message)}`);
    return;
  }
  renderAll();
  $('settingsTitle').focus();
  fetchVersions().then((list) => { versions = list; updateChrome(); }).catch(() => {});
  if (sectionId) goto(sectionId);
}

function goto(id) {
  const target = $(`set-sec-${id}`);
  if (!target) return;
  if (target.tagName === 'DETAILS') target.open = true;
  target.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  $('settingsJump').value = id;
}

function reallyClose() {
  draftSource = '';
  stopPreview();
  $('settingsPreview').hidden = true;
  clearDraft();
  closeModal(MODAL);
  $('settingsSections').replaceChildren(); // about 5.7k nodes that nothing needs while closed; openSettings builds them again
  hideConfirm();
  if (guarded) { // take the entry guardBack added away again, so one Back leaves Settings
    guarded = false;
    if (location.hash === '#settings') { // its hashchange is ours: the dialog may already be open again by then
      ownBack = true;
      setTimeout(() => { ownBack = false; }, 1000);
      history.back();
    }
  } else if (location.hash === '#settings') history.replaceState(null, '', `${location.pathname}${location.search}`);
  const back = opener && opener.isConnected && opener.offsetParent !== null ? opener : $('btnSettings');
  opener = null;
  back?.focus();
}

function askConfirm(text, yesLabel, fn) {
  confirmed = fn;
  $('settingsConfirmText').textContent = text;
  $('btnConfirmYes').textContent = yesLabel;
  if ($('settingsConfirm').hidden) returnFocus = document.activeElement;
  $('settingsConfirm').hidden = false;
  $('btnConfirmNo').focus();
}
function hideConfirm() { confirmed = null; $('settingsConfirm').hidden = true; }

// "Keep editing" (and Escape on the question): back to what had focus, else the Cancel button, so the keyboard goes on.
function keepEditing() {
  const usable = returnFocus && $(MODAL).contains(returnFocus) && !returnFocus.disabled && returnFocus.offsetParent !== null;
  const back = usable ? returnFocus : $('btnSettingsCancel');
  returnFocus = null;
  hideConfirm();
  back.focus();
}

// Closing with a draft asks first (Cancel, the close button, Escape).
function requestClose() {
  const n = changeCount();
  if (!isOpen()) return;
  if (!n) { reallyClose(); return; }
  askConfirm(`Discard ${n} unsaved change${n === 1 ? '' : 's'}?`, 'Discard', reallyClose);
}

// Replacing the draft (a version, an imported file) asks when there is one.
function replaceDraft(text, fn) {
  if (changeCount() === 0) { fn(); return; }
  askConfirm(text, 'Replace the draft', fn);
}

// ---------- Save ----------
const sentence = (names) => names.map((n) => (S.spec[n] ? `${S.spec[n].group ? `${S.spec[n].group}: ` : ''}${S.spec[n].label}` : n)).join(', ');

async function save() {
  if (saving || !changeCount() || hasProblems()) return;
  saving = true;
  updateChrome();
  const body = { base_revision: S.server.revision, values: S.draft.values, lists: S.draft.lists, secrets: S.draft.secrets };
  try {
    const res = await fetch(`/api/settings${draftSource ? `?source=${draftSource}` : ''}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (res.ok) await saved(data);
    else if (res.status === 409) await conflicted(data);
    else if (res.status === 400 || res.status === 422) {
      showServerErrors(data.errors || [], []);
      addNote('crit', `<b>Not saved.</b> ${escapeHtml(data.detail || 'The server refused the change.')}`);
    } else addNote('crit', `<b>Not saved.</b> ${escapeHtml(data.detail || `The server answered ${res.status}.`)}`);
  } catch (e) {
    addNote('crit', '<b>Not saved.</b> The server could not be reached.');
  } finally {
    saving = false;
    updateChrome();
  }
}

async function saved(result) {
  const applied = result.applied || [];
  toast(result.summary || 'Settings saved');
  const after = [];
  if ((result.live_failed || []).length) after.push({ level: 'warn', html: `<b>Saved, but not applied yet:</b> ${escapeHtml(sentence(result.live_failed))}. It applies at the next start.` });
  if ((result.restart_needed || []).length) after.push({ level: 'warn', html: `<b>Applies after a restart:</b> ${escapeHtml(sentence(result.restart_needed))}.` });
  clearDraft();
  draftSource = '';
  stopPreview();
  try { await fetchState(); } catch (e) { after.push({ level: 'warn', html: 'Saved. The page could not re-read the settings; reopen it.' }); }
  setNotes(after);
  if (S.server) { renderAll(); fetchVersions().then((list) => { versions = list; renderVersions(); updateChrome(); }).catch(() => {}); }
  refreshSettingsValues();
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
  const dropped = dropConflicts(conflicts);
  setNotes([{ level: 'warn', html: `<b>Changed elsewhere; kept theirs:</b> ${escapeHtml(sentence(dropped.length ? dropped : conflicts))}. Your other changes are still here: press Save again.` }]);
  renderAll();
  changed();
}

// ---------- versions, import ----------
function renderVersions() {
  const box = $('settingsVersions');
  box.hidden = !versionsShown;
  if (versionsShown && versions) box.innerHTML = versionsHtml(versions, S.server.revision);
}

// The versions and the export open at the top of the dialog body: bring them into view, wherever the body was scrolled
// to (opened from the footer while scrolled down, they would appear out of sight, and the button seem to do nothing).
function reveal(box) {
  box.scrollIntoView({ block: 'start' });
  box.focus({ preventScroll: true });
}

async function toggleVersions() {
  versionsShown = !versionsShown;
  $('btnSettingsVersions').setAttribute('aria-expanded', String(versionsShown));
  if (versionsShown && !versions) {
    $('settingsVersions').hidden = false;
    $('settingsVersions').innerHTML = '<p class="fine">Loading…</p>';
    reveal($('settingsVersions'));
    try { versions = await fetchVersions(); } catch (e) { $('settingsVersions').innerHTML = `<p class="fine">${escapeHtml(e.message)}</p>`; return; }
  }
  renderVersions();
  if (versionsShown) reveal($('settingsVersions'));
}

// Export shows the YAML here, with Copy and a download link. Chrome blocks or warns about a file downloaded over plain
// HTTP from another machine ("insecure download"), whatever the page does; Copy works on every connection.
function showExport(on) {
  exportShown = on;
  $('settingsExport').setAttribute('aria-expanded', String(on));
  $('settingsExportView').hidden = !on;
  if (!on) $('settingsExportView').innerHTML = '';
}

async function toggleExport() {
  showExport(!exportShown);
  if (!exportShown) return;
  const box = $('settingsExportView');
  box.innerHTML = '<p class="fine">Loading…</p>';
  reveal(box);
  let text;
  try {
    const res = await fetch('/api/settings/export', { cache: 'no-store' });
    if (!res.ok) throw new Error(`The settings could not be exported (${res.status}).`);
    text = await res.text();
  } catch (e) {
    box.innerHTML = `<p class="fine">${escapeHtml(e.message)}</p>`;
    return;
  }
  box.innerHTML = exportViewHtml({ prefix: 'settings', intro: 'Every setting and list as YAML, without secrets. Import reads it in another install.',
    text, href: '/api/settings/export', file: 'pengumon-settings.yaml' });
  reveal(box);
}

async function loadVersion(n) {
  try {
    const doc = await fetchVersion(n);
    replaceDraft(`Replace the draft with revision ${n}?`, () => {
      applyDocument(doc);
      draftSource = 'restore';
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
    if (!res.ok) throw new Error(data.detail || `The server answered ${res.status}.`);
    applyDocument(data);
    draftSource = 'yaml_import';
    setNotes([{ level: 'unknown', html: `<b>Read ${escapeHtml(name)}.</b> It is a draft: review it, then Save.` }, ...(data.notes || []).map((t) => ({ level: 'warn', html: escapeHtml(t) }))]);
    renderAll();
  } catch (e) {
    addNote('crit', `<b>The file was not imported.</b> ${escapeHtml(e.message)}`);
  }
}

async function importFile(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  const text = await file.text();
  replaceDraft(`Replace the draft with ${file.name}?`, () => importText(file.name, text));
}

// ---------- actions ----------
function useBrowserLayout() {
  const mine = browserLayout();
  setValue('dashboard.order', mine.order);
  setValue('dashboard.hidden', mine.hidden);
  refreshRows();
  toast('This browser\'s layout is in the draft. Save to make it the default.');
}

function jumpToRow(key) {
  const row = document.querySelector(`.set-row[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  const adv = row.closest('details');
  if (adv) adv.open = true;
  row.scrollIntoView({ block: 'center' });
  row.classList.add('flash');
  setTimeout(() => row.classList.remove('flash'), 1200);
}

function confirmYes() {
  const fn = confirmed;
  hideConfirm();
  if (fn) fn();
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
  settingsConfirmYes: () => confirmYes(),
  settingsConfirmNo: () => keepEditing(),
  settingsPreviewCancel: () => { cancelPreview(); $('settingsPreview').querySelector('[data-action=settingsPreviewRun]')?.focus(); },
  settingsVersions: () => toggleVersions(),
  settingsExport: () => toggleExport(),
  settingsExportCopy: () => copyExport('settings'),
  settingsLoadVersion: (el) => loadVersion(el.dataset.arg),
  settingsImport: () => $('settingsImportFile').click(),
  settingsUseLayout: () => useBrowserLayout(),
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
  // a sign-in adds what only an editor is sent (the install's paths)
  getJson('/api/settings').then((server) => { S.server = server; renderAll(); }).catch(() => {});
});

// Keyboard: focus moves into the dialog when it opens, Tab stays inside it and Escape closes it (asking first when
// there is a draft).
function keepKeyboard(modal) {
  // on the document: after a click on a button that went away (Keep editing, Discard) focus is on <body>, outside the modal
  document.addEventListener('keydown', (ev) => {
    if (!isOpen() || !(modal.contains(ev.target) || ev.target === document.body)) return;
    if (ev.key === 'Escape' && !ev.defaultPrevented) {
      ev.preventDefault();
      if (!$('settingsConfirm').hidden) keepEditing(); else requestClose();
      return;
    }
    if (ev.key !== 'Tab') return;
    const items = [...modal.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]')].filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (!modal.contains(document.activeElement)) { ev.preventDefault(); (ev.shiftKey ? last : first).focus(); }
    else if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });
}
keepKeyboard($(MODAL));

function openFromHash() {
  if (location.hash === '#settings' && !isOpen()) openSettings();
}
// Back (or any change of the address) while the dialog is open: with a draft, stay and ask; without, close.
window.addEventListener('hashchange', () => {
  if (ownBack) {
    ownBack = false;
    if (isOpen() && location.hash !== '#settings') history.replaceState(null, '', `${location.pathname}${location.search}#settings`);
    return;
  }
  if (location.hash === '#settings') { openFromHash(); return; }
  if (!isOpen()) return;
  if (changeCount()) {
    history.pushState(null, '', `${location.pathname}${location.search}#settings`);
    requestClose();
  } else { guarded = false; reallyClose(); } // Back already took the guard entry
});
// Reload, another address or closing the tab while there is a draft: the browser asks.
window.addEventListener('beforeunload', (ev) => {
  if (isOpen() && changeCount()) { ev.preventDefault(); ev.returnValue = ''; }
});
openFromHash();
