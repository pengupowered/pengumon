// The markup of the page: the sections of the schema with their groups, rows, secret slots and list boxes, and the
// About section. Pure functions of the state (store.js); mountLists (render.js) binds the list editors to the boxes.

import { escapeHtml } from '../../js/escape.js';
import { icon } from '../../js/dom.js';
import { accessHtml, viewersConfirmHtml } from './access.js';
import { isLayoutKey, layoutRowHtml, rowHtml, speedEstimateRowHtml, speedMlabRowHtml } from './fields.js';
import { rememberSlot, slotHtml } from './secrets.js';
import { S } from './store.js';

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
  return name === 'devices' ? `<div class="set-list"><div class="set-list-h"><h4 class="set-group-t">Devices</h4></div>${box}</div>` : box;
}

// The heading a list editor draws, by where it sits: a group of its own (level of a group heading), inside a group
// (one level below), or none where the group it sits in already has its name.
export const LIST_HEADING = { dns_probes: 'group', webhooks: 'sub', services: 'none' };

function secretsHtml(section) {
  return S.schema.secrets.filter((s) => s.section === section && !s.pattern).map((s) => { rememberSlot(s.slot, s); return slotHtml(s.slot, s); }).join('');
}

function groupHtml(section, group) {
  // dashboard.order and dashboard.hidden are one row, "Layout for new browsers", where the first of them stands
  const rows = group.specs.map((s) => (!isLayoutKey(s.key) ? rowHtml(s, section.id) : s.key === 'dashboard.order' ? layoutRowHtml() : '')).join('');
  let extra = '';
  if (section.id === 'alerts' && group.name === 'Channels') extra = secretsHtml('alerts') + listHtml('webhooks');
  if (section.id === 'devices' && group.name === 'Host app checks') extra = listHtml('services');
  if (section.id === 'speedtest' && !group.name) extra = speedMlabRowHtml();
  if (section.id === 'speedtest' && group.name === 'Data use') extra = speedEstimateRowHtml();
  if (group.specs.some((s) => s.key === 'access.allow_viewers')) extra = viewersConfirmHtml(); // its current password
  return `<div class="set-group">${group.name ? `<h4 class="set-group-t">${escapeHtml(group.name)}</h4>` : ''}${rows}${extra}</div>`;
}

export function sectionHtml(section) {
  let groups = groupsOf(section);
  let head = '';
  let tail = '';
  if (section.id === 'devices') head = listHtml('devices');
  if (section.id === 'access') head = accessHtml();
  if (section.id === 'probing') tail = listHtml('dns_probes');
  if (section.id === 'display') tail = listHtml('themes');
  if (section.id === 'alerts' && !groups.some((g) => g.name === 'Channels')) groups = [{ name: 'Channels', specs: [] }, ...groups];
  if (section.id === 'devices' && !groups.some((g) => g.name === 'Host app checks')) groups.push({ name: 'Host app checks', specs: [] });
  const body = `${head}${groups.map((g) => groupHtml(section, g)).join('')}${tail}`;
  if (section.id === 'advanced') {
    return `<details class="set-adv" id="set-sec-advanced" data-section="advanced"><summary>${icon('chevron', 'set-chev')}<h3 class="section-t">${escapeHtml(section.title)}</h3><span class="muted">Change these only if you know why.</span></summary>${body}</details>`;
  }
  return `<section class="set-sec" id="set-sec-${escapeHtml(section.id)}" data-section="${escapeHtml(section.id)}" aria-labelledby="set-sec-${escapeHtml(section.id)}-t"><h3 class="section-t" id="set-sec-${escapeHtml(section.id)}-t">${escapeHtml(section.title)}</h3>${body}</section>`;
}

export function aboutHtml() {
  const a = S.server.about;
  if (!a) {
    return `<section class="set-sec" id="set-sec-about" data-section="about"><h3 class="section-t">About this install</h3><p class="fine top">What stays outside this page (the listen address, the data folder, the config file) is shown to people signed in.</p></section>`;
  }
  const rows = [
    ['Version', a.version], ['Listens on', a.listen], ['HTTPS', location.protocol === 'https:' ? 'on' : 'off (plain HTTP)'], ['Data folder', a.data_dir],
    ['Config file', a.config_path], ['Database', a.db_path || '—'], ['Metrics export', a.otlp_host || 'off']
  ];
  const cfg = a.config_yaml && a.config_yaml.differing_keys ? `<p class="fine">The config file only set the first values: ${Number(a.config_yaml.differing_keys)} of its value${a.config_yaml.differing_keys === 1 ? '' : 's'} now differ${a.config_yaml.differing_keys === 1 ? 's' : ''} from this page, and this page wins. Edit settings here, not in the file.</p>` : '';
  const report = (a.import_report || []).length ? `<details><summary class="fine">The first import left ${a.import_report.length} thing${a.import_report.length === 1 ? '' : 's'} out</summary><ul class="fine">${a.import_report.map((r) => `<li>${escapeHtml(r.key)}: ${escapeHtml(r.reason)}</li>`).join('')}</ul></details>` : '';
  // A plain link, not a fetch: the file can be hundreds of MB and goes to disk, not into the page's memory.
  const backup = `<p class="top"><a class="btn sm" id="settingsBackup" data-backup href="/api/backup" download><svg class="i" aria-hidden="true"><use href="#i-download"/></svg><span>Download a backup of the database</span></a></p>
    <p class="fine">The history and the settings, with no passwords or webhook addresses. To restore it, stop Pengumon, replace the database file with it and start again: see Backups in docs/storage.md.</p>`;
  return `<section class="set-sec" id="set-sec-about" data-section="about"><h3 class="section-t">About this install</h3>
    <dl class="set-about">${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('')}</dl>${cfg}${report}${backup}</section>`;
}
