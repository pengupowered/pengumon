// One setting as a row: label, input, unit, and under it the effect, the standard value, the badges and "Reset". The
// inputs are built from the schema (type, range, choices); a value is shown in the schema's unit and stored in the
// server's. Every value on the page is put into words by one function, `shownValue` (units, choice labels, zones,
// "none"), and every row's standard value by `standardText`.

import { escapeHtml } from '../../js/util.js';
import { panelTitles } from '../../js/layout.js';
import { HOUR_UNITS, choiceFor, fmt, hoursText, isNumeric, parse, problem, readHours, same, speedEstimate, splitHours, toShown } from './model.js';
import { S, isEnv, inDraft, listItems, roundSeconds, setValue, valueOf, valueState } from './store.js';

const ISP = 'classifier.isp_latency_spike_threshold_ms';
const ZONE = 'general.timezone';
const LAYOUT_KEYS = ['dashboard.order', 'dashboard.hidden', 'dashboard.colors'];

const inputId = (section, key) => `set-in-${section}-${key.replace(/[^a-z0-9]+/gi, '-')}`;
const num = (spec, stored) => (stored === null || stored === undefined ? '' : String(toShown(spec, stored, roundSeconds())));

// ---------- values in words ----------

// The names a UTC clock goes by (pengumon.timeofday.UTC_NAMES): all are shown as "UTC".
const UTC_NAMES = new Set(['UTC', 'UCT', 'GMT', 'GMT0', 'GMT+0', 'GMT-0', 'Greenwich', 'Universal', 'Zulu', '+00:00', '-00:00',
  'Etc/UTC', 'Etc/UCT', 'Etc/GMT', 'Etc/GMT0', 'Etc/GMT+0', 'Etc/GMT-0', 'Etc/Greenwich', 'Etc/Universal', 'Etc/Zulu']);
const isUtc = (zone) => !zone || UTC_NAMES.has(zone);

// "New York (America/New_York)", "Buenos Aires, Argentina (America/Argentina/Buenos_Aires)", "UTC", "UTC+01:00 (fixed
// offset)"; Etc/GMT+5 is five hours *behind* UTC (the POSIX sign), "UTC-05:00 (fixed offset)".
export function zoneLabel(zone) {
  if (isUtc(zone)) return 'UTC';
  if (/^[+-]\d\d:\d\d$/.test(zone)) return `UTC${zone} (fixed offset)`;
  const etc = /^Etc\/GMT([+-])(\d{1,2})$/.exec(zone);
  if (etc) return `UTC${etc[1] === '+' ? '-' : '+'}${etc[2].padStart(2, '0')}:00 (fixed offset)`;
  const parts = zone.split('/');
  if (parts.length === 1) return zone.replace(/_/g, ' ');
  return `${parts.slice(1).reverse().join(', ').replace(/_/g, ' ')} (${zone})`;
}

// The short name of a zone for a sentence: "New York", "UTC", "UTC+01:00".
function zoneShort(zone) {
  if (isUtc(zone)) return 'UTC';
  if (/^[+-]\d\d:\d\d$/.test(zone)) return `UTC${zone}`;
  return zoneLabel(zone).replace(/ \(.*\)$/, '');
}

function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { return ''; }
}

// "22:00" as this browser shows times (its time boxes too): "10:00 PM" in the US, "22:00" elsewhere.
function clockText(hhmm) {
  const m = /^(\d\d):(\d\d)$/.exec(String(hhmm));
  if (!m) return String(hhmm);
  try {
    return new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' }).format(Date.UTC(2000, 0, 1, Number(m[1]), Number(m[2])));
  } catch (e) { return String(hhmm); }
}

const choiceLabel = (spec, choice) => (spec.choice_labels && spec.choice_labels[choice]) || String(choice);

// The panel layout a browser starts with, in words, by the panels' titles.
function layoutSummary(order, hidden, colors) {
  const titles = panelTitles();
  const ids = [...titles.keys()];
  const name = (id) => titles.get(id) || id;
  const known = (list) => (Array.isArray(list) ? list.filter((id) => titles.has(id)) : []);
  const merged = [...new Set([...known(order), ...ids])];
  const standardOrder = merged.every((id, i) => id === ids[i]);
  const off = known(hidden);
  const own = colors && typeof colors === 'object' ? Object.keys(colors).filter((id) => titles.has(id)) : [];
  const tail = own.length ? ` · Own colors: ${own.map(name).join(', ')}` : '';
  if (standardOrder && !off.length) return `All panels, standard order${tail}`;
  const shown = merged.filter((id) => !off.includes(id)).map(name);
  return `${standardOrder ? 'Standard order' : `Order: ${shown.join(', ')}`} · ${off.length ? `Hidden: ${off.map(name).join(', ')}` : 'No panel hidden'}${tail}`;
}

// The themes Display > Theme offers: the built-in ones (the schema's choice labels), then the custom ones of the draft.
function themeChoices(spec) {
  const custom = listItems('themes').filter((t) => t && t.id).map((t) => [t.id, `${t.name || t.id} (custom)`]);
  return [...Object.entries(spec.choice_labels || {}), ...custom];
}
const themeName = (spec, id) => (themeChoices(spec).find(([k]) => k === id) || [id, id])[1];
function themeOptions(spec, value) {
  const choices = themeChoices(spec);
  if (!choices.some(([id]) => id === value)) choices.push([value, `${value} (not found: the standard theme shows)`]);
  return choices.map(([id, name]) => `<option value="${escapeHtml(id)}"${id === value ? ' selected' : ''}>${escapeHtml(name)}</option>`).join('');
}

// A stored value of `spec` as the page shows it: "15 %", "5 min", "1 per million", "Outages only", "on", "New York
// (America/New_York)", or what an empty value means ("learned from the router's traffic", else "none").
export function shownValue(spec, v) {
  if (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length)) return spec.empty || 'none';
  if (spec.type === 'choice') return choiceLabel(spec, v);
  if (spec.type === 'theme') return themeName(spec, v);
  if (spec.type === 'timezone') return zoneLabel(v);
  if (spec.type === 'time') return clockText(v);
  if (spec.type === 'hours') return hoursText(v);
  return fmt(spec, v, roundSeconds());
}

// "Standard: 15 %": the one form of a row's standard (default) value.
export function standardText(spec) {
  if (spec.key === ISP) {
    const pub = S.spec['classifier.public_latency_spike_threshold_ms'];
    return `Standard: same as internet (${shownValue(pub, valueOf(pub.key))})`;
  }
  if (spec.type === 'timezone') return `Standard: this server's zone, ${zoneLabel(spec.default)}`;
  return `Standard: ${shownValue(spec, spec.default)}`;
}

// ---------- inputs ----------

// The time zone choices: the ones that matter here first (this browser's, the server's, the current one), then every
// zone the browser knows, grouped by region and sorted by place. The UTC names are one entry, "UTC".
function zoneOptions(current) {
  let all = [];
  try { all = Intl.supportedValuesOf('timeZone'); } catch (e) { all = []; }
  const utc = isUtc(current) ? current : 'UTC';
  const same = (z) => (isUtc(z) ? utc : z);
  const mine = browserZone();
  const server = S.spec[ZONE] ? S.spec[ZONE].default : '';
  const notes = new Map();
  [[mine, 'your browser'], [server, 'server'], [current, '']].forEach(([zone, note]) => {
    if (!zone) return;
    const z = same(zone);
    const had = notes.get(z);
    notes.set(z, had === undefined ? note : [had, note].filter(Boolean).join(' and '));
  });
  const opt = (z) => `<option value="${escapeHtml(z)}"${z === current ? ' selected' : ''}>${escapeHtml(zoneLabel(z))}</option>`;
  // the suggested ones by city and a short note ("New York · your browser and server"), so the box shows it whole
  const city = (z) => zoneLabel(z).replace(/ \([^)]*\)$/, '');
  const top = [...notes].map(([z, note]) => `<option value="${escapeHtml(z)}"${z === current ? ' selected' : ''}>${escapeHtml(city(z))}${note ? ` · ${escapeHtml(note)}` : ''}</option>`).join('');
  const regions = new Map();
  [...new Set(all.map(same).concat(utc))].filter((z) => !notes.has(z)).forEach((z) => {
    const region = isUtc(z) || !z.includes('/') || z.startsWith('Etc/') ? 'Other' : z.split('/')[0];
    if (!regions.has(region)) regions.set(region, []);
    regions.get(region).push(z);
  });
  const groups = [...regions.keys()].sort((a, b) => (a === 'Other') - (b === 'Other') || a.localeCompare(b));
  return `<optgroup label="Suggested">${top}</optgroup>${groups.map((g) => `<optgroup label="${escapeHtml(g)}">${regions.get(g)
    .map((z) => [zoneLabel(z), z]).sort((a, b) => a[0].localeCompare(b[0])).map(([, z]) => opt(z)).join('')}</optgroup>`).join('')}`;
}

function inputHtml(spec, section) {
  const id = inputId(section, spec.key);
  const value = valueOf(spec.key);
  const env = isEnv(spec.key);
  const common = `id="${id}" data-key="${escapeHtml(spec.key)}" data-input="settingsEdit" data-change="settingsEdit"`;
  const locked = env ? ' readonly aria-readonly="true"' : '';
  if (spec.type === 'boolean') {
    return `<label class="switch"><input type="checkbox" role="switch" ${common}${value ? ' checked' : ''}${env ? ' disabled' : ''} aria-labelledby="${id}-l"><span></span></label>`;
  }
  if (spec.type === 'choice') {
    const choices = spec.choices || [];
    if (choices.length <= 3) {
      return `<span class="seg" role="group" aria-labelledby="${id}-l" id="${id}">${choices.map((c) =>
        `<button type="button" data-action="settingsChoice" data-key="${escapeHtml(spec.key)}" data-arg="${escapeHtml(c)}" aria-pressed="${String(c) === String(value)}"${env ? ' disabled' : ''}>${escapeHtml(choiceLabel(spec, c))}</button>`).join('')}</span>`;
    }
    return `<select class="input" ${common}${env ? ' disabled' : ''}>${choices.map((c) => `<option value="${escapeHtml(c)}"${String(c) === String(value) ? ' selected' : ''}>${escapeHtml(choiceLabel(spec, c))}</option>`).join('')}</select>`;
  }
  if (spec.type === 'timezone') {
    return `<select class="input" ${common}${env ? ' disabled' : ''}>${zoneOptions(value)}</select>`;
  }
  if (spec.type === 'theme') {
    return `<select class="input" ${common}${env ? ' disabled' : ''}>${themeOptions(spec, value)}</select>`;
  }
  if (spec.type === 'time') {
    return `<input class="input" type="time" ${common}${locked} value="${escapeHtml(value || '')}">`;
  }
  if (spec.type === 'hours') { // a whole number and its unit: "2" "weeks"; both edit the one stored number of hours
    const { count, unit } = splitHours(value);
    return `<span class="set-hours"><input class="input num" type="text" inputmode="numeric" autocomplete="off" ${common}${locked} value="${escapeHtml(count)}" aria-describedby="${id}-m">`
      + `<select class="input" data-key="${escapeHtml(spec.key)}" data-unit data-change="settingsEdit" aria-label="Unit"${env ? ' disabled' : ''}>${HOUR_UNITS.map(([u]) =>
        `<option value="${u}"${u === unit ? ' selected' : ''}>${u}</option>`).join('')}</select></span>`;
  }
  if (isNumeric(spec)) {
    return `<input class="input num" type="text" inputmode="decimal" autocomplete="off" ${common}${locked} value="${escapeHtml(num(spec, value))}" placeholder="${escapeHtml(spec.empty || 'none')}" aria-describedby="${id}-m">`;
  }
  return `<input class="input" type="text" autocomplete="off" spellcheck="false" ${common}${locked} value="${escapeHtml(value ?? '')}"${spec.max_length ? ` maxlength="${spec.max_length}"` : ''} placeholder="${escapeHtml(spec.empty || 'none')}" aria-describedby="${id}-m">`;
}

// "It is 9:40 PM there now (New York).": the clock in the time zone that quiet hours are read in.
function zoneClock() {
  const zone = String(valueOf(ZONE) || '') || 'UTC';
  try {
    const time = new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit', timeZone: isUtc(zone) ? 'UTC' : zone }).format(new Date());
    return `<span class="set-now" data-now>It is ${escapeHtml(time)} there now (${escapeHtml(zoneShort(zone))}).</span>`;
  } catch (e) { return ''; }
}

// "Use this browser's zone (America/New_York)", when the setting is another zone.
function useZoneHtml() {
  const mine = browserZone();
  const now = String(valueOf(ZONE) || '');
  if (!mine || isEnv(ZONE) || mine === now || (isUtc(mine) && isUtc(now))) return '';
  return `<button type="button" class="set-lnk" data-action="settingsUseZone" data-arg="${escapeHtml(mine)}" data-needs-edit>Use this browser's zone (${escapeHtml(mine)})</button>`;
}

// The badges and Reset of a row (or of the layout row, which stands for two keys).
function stateHtml(keys, resetHtml) {
  const env = keys.some(isEnv);
  const unsaved = keys.some(inDraft);
  // a stored value can equal the default (the import keeps every value config.yaml set): not a change, so no badge and no Reset
  const differs = keys.some((key) => {
    const spec = S.spec[key];
    const v = inDraft(key) ? S.draft.values[key] : (valueState(key).stored ?? null);
    return v !== null && !same(v, spec.default);
  });
  const chips = [];
  if (env) chips.push(`<span class="chip st st-unknown" data-badge="env">From environment · ${escapeHtml(keys.map((k) => S.spec[k].env).filter(Boolean).join(', '))}</span>`);
  else {
    if (differs) chips.push('<span class="chip st st-busy" data-badge="changed">Changed</span>');
    if (unsaved) chips.push('<span class="chip st st-warn" data-badge="unsaved">Unsaved</span>');
  }
  const states = keys.map(valueState);
  if (states.some((s) => s.pending_restart)) chips.push('<span class="chip st st-warn" data-badge="restart">Applies after a restart</span>');
  if (states.some((s) => s.live_failed)) chips.push('<span class="chip st st-crit" data-badge="live-failed">Saved; applies at the next start</span>');
  return `${chips.join(' ')} ${!env && differs ? resetHtml : ''}`; // a value equal to the default has nothing to reset
}

// The line under the input: the effect on the left; the standard value, badges and Reset under the input; then a
// warning, the clock of the time zone. Rebuilt after every edit; the input itself is not touched.
export function metaHtml(spec) {
  const reset = `<button type="button" class="set-lnk" data-action="settingsReset" data-key="${escapeHtml(spec.key)}" data-needs-edit>Reset to standard</button>`;
  const warn = S.warnings.filter((w) => w.where === `values.${spec.key}`).map((w) => `<span class="set-warn">${escapeHtml(w.message)}</span>`).join('');
  const clock = spec.key === 'alerts.quiet_hours.start' || spec.key === 'alerts.quiet_hours.end' || spec.key === ZONE ? zoneClock() : '';
  const zone = spec.key === ZONE ? useZoneHtml() : '';
  const extra = clock || zone ? `<span class="set-extra">${clock} ${zone}</span>` : '';
  return `<span class="set-effect">${escapeHtml(spec.effect)}</span><span class="set-side"><span class="set-def">${escapeHtml(standardText(spec))}</span> ${stateHtml([spec.key], reset)}${extra}</span>${warn}`;
}

// A row that relates to a setting elsewhere (a hop color to its incident level) names it, its value, and links to it.
function relatedHtml(spec) {
  const other = spec.related && S.spec[spec.related];
  if (!other) return '';
  const section = (S.schema.sections.find((s) => s.id === other.section) || {}).title || '';
  const grouped = other.group && !other.label.toLowerCase().includes(other.group.split(' ')[0].toLowerCase()); // "Latency spike levels: Router"
  const name = `${grouped ? `${other.group}: ` : ''}${other.label}`;
  return `<span class="set-rel">Related: ${escapeHtml(name)}${section ? ` (${escapeHtml(section)})` : ''} is <b data-rel="${escapeHtml(other.key)}">${escapeHtml(shownValue(other, valueOf(other.key)))}</b>.
    <button type="button" class="set-lnk" data-action="settingsJump" data-arg="${escapeHtml(other.key)}">Go to that setting</button></span>`;
}

export function messageText(key) {
  return S.invalid[key] || S.errors[`values.${key}`] || '';
}

export function rowHtml(spec, section) {
  const id = inputId(section, spec.key);
  const unit = isNumeric(spec) && spec.unit ? `<span class="set-unit">${escapeHtml(spec.unit)}</span>` : '';
  return `<div class="set-row" data-key="${escapeHtml(spec.key)}" data-type="${spec.type}" id="${id}-row">
    <div class="set-main">
      <label class="set-label" id="${id}-l" for="${id}">${escapeHtml(spec.label)}</label>${relatedHtml(spec)}
      <div class="set-input">${inputHtml(spec, section)}${unit}</div>
    </div>
    <div class="set-meta" id="${id}-m" data-meta>${metaHtml(spec)}</div>
    <p class="set-msg" data-msg role="alert">${escapeHtml(messageText(spec.key))}</p>
  </div>`;
}

// ---------- the layout row: dashboard.order and dashboard.hidden as one ----------

const layoutNow = () => layoutSummary(valueOf('dashboard.order'), valueOf('dashboard.hidden'), valueOf('dashboard.colors'));

function layoutMetaHtml() {
  const reset = '<button type="button" class="set-lnk" data-action="settingsResetLayout" data-needs-edit>Reset to standard</button>';
  return `<span class="set-effect">The panel order, hidden panels and card colors a browser starts with. Once someone arranges a browser with Layout, that browser keeps its own.</span><span class="set-side"><span class="set-def">Standard: all panels, standard order</span> ${stateHtml(LAYOUT_KEYS, reset)}</span>`;
}

export const isLayoutKey = (key) => LAYOUT_KEYS.includes(key);

// ---------- the speed test's extra lines: what it uses a month, and what M-Lab publishes ----------
// Two lines that are not settings: the estimate under the data fields (it follows the draft as the fields change) and,
// while M-Lab is the choice, its privacy note in words under the provider (the choice's label says it too, but a
// closed box shows a cut label on a phone).
const estimateNow = () => speedEstimate(valueOf('speedtest.interval_hours'), valueOf('speedtest.download_mb'), valueOf('speedtest.upload_mb'), valueOf('speedtest.monthly_cap_mb'), valueOf('line.down_mbps'), valueOf('line.up_mbps'));
export const MLAB_NOTE = 'M-Lab publishes every result, with your public IP address, as open data.';

export function speedEstimateRowHtml() {
  return `<div class="set-row set-note" data-extra="speed-estimate"><p class="set-estimate" data-estimate aria-live="polite">${escapeHtml(estimateNow())}</p></div>`;
}

export function speedMlabRowHtml() {
  return `<div class="set-row set-note" data-extra="speed-mlab"${valueOf('speedtest.provider') === 'mlab' ? '' : ' data-inactive'}><p class="set-privacy"><b>Privacy:</b> <span data-privacy>${escapeHtml(MLAB_NOTE)}</span></p></div>`;
}

export function layoutRowHtml() {
  const env = LAYOUT_KEYS.some(isEnv);
  return `<div class="set-row" data-extra="layout" data-keys="${LAYOUT_KEYS.join(' ')}" id="set-layout-row">
    <div class="set-main">
      <span class="set-label" id="set-layout-l">Layout for new browsers</span>
      <div class="set-input set-input-layout"><span class="set-layout" data-layout>${escapeHtml(layoutNow())}</span>
        <button type="button" class="btn sm outline" data-action="settingsUseLayout" data-needs-edit${env ? ' disabled' : ''} aria-describedby="set-layout-l">Use this browser's layout</button></div>
    </div>
    <div class="set-meta" data-meta-layout>${layoutMetaHtml()}</div>
  </div>`;
}

// ---------- reading and editing ----------

// The input of `el` read as a stored value: {stored} or {problem}.
export function readInput(el, spec) {
  if (spec.type === 'boolean') return { stored: el.checked };
  if (spec.type === 'hours') { // the number and the unit are read together, whichever of the two changed
    const box = el.closest('.set-hours');
    return readHours(box.querySelector('input').value, box.querySelector('select').value, spec.max);
  }
  const text = el.value;
  const why = problem(spec, text, roundSeconds());
  if (why) return { problem: why };
  if (spec.type === 'choice') return { stored: choiceFor(spec, text) };
  return { stored: spec.type === 'timezone' || spec.type === 'theme' ? text : parse(spec, text, roundSeconds()) };
}

// An edit of a row's input (typing, switching, picking).
export function edit(el) {
  const key = el.dataset.key;
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const got = readInput(el, spec);
  if (got.problem) {
    S.invalid[key] = got.problem;
    refreshRows();
    return;
  }
  setValue(key, got.stored); // clears an earlier problem; the page then refreshes the rows
}

export function choose(key, choice) {
  const spec = S.spec[key];
  if (spec && !isEnv(key)) setValue(key, spec.type === 'choice' ? choiceFor(spec, choice) ?? choice : choice);
}

// "Use this browser's zone": the draft takes it, and focus goes to the zone's box (the link itself goes away).
export function useZone(zone, from) {
  if (isEnv(ZONE)) return;
  const box = from?.closest('.set-row')?.querySelector('select[data-key]'); // found first: the redraw takes the link away
  setValue(ZONE, zone);
  box?.focus();
}

export function resetLayout(from) {
  const use = from?.closest('.set-row')?.querySelector('[data-action=settingsUseLayout]');
  LAYOUT_KEYS.forEach((key) => setValue(key, null));
  use?.focus();
}

// Redraws a meta line only when its content changed, and keeps focus on its "Reset to standard" if that had it: a redraw
// on every edit dropped keyboard focus to <body> for a Tab stop (the button the Tab was heading to was replaced).
function paintMeta(meta, html) {
  if (meta._html === html) return;
  const had = meta.contains(document.activeElement) ? document.activeElement.dataset.action : null;
  meta.innerHTML = html;
  meta._html = html;
  if (had) meta.querySelector(`[data-action="${had}"]`)?.focus();
}

const rows = (root) => [...root.querySelectorAll('.set-row[data-key]')];

// Brings the parts that depend on state up to date: every meta line, message and related value, and the inputs of
// rows that are not being typed in (a Reset, or the same key shown in two sections).
export function refreshRows(root = document.getElementById('settingsModal')) {
  rows(root).forEach((row) => {
    const spec = S.spec[row.dataset.key];
    if (!spec) return;
    paintMeta(row.querySelector('[data-meta]'), metaHtml(spec));
    row.querySelector('[data-msg]').textContent = messageText(spec.key);
    const input = row.querySelector('input[data-key], select[data-key]');
    const bad = !!S.invalid[spec.key];
    row.classList.toggle('bad', bad || !!S.errors[`values.${spec.key}`]);
    row.classList.toggle('unsaved', inDraft(spec.key));
    if (input) input.setAttribute('aria-invalid', String(bad || !!S.errors[`values.${spec.key}`]));
    const rel = row.querySelector('[data-rel]');
    if (rel) rel.textContent = shownValue(S.spec[rel.dataset.rel], valueOf(rel.dataset.rel));
    if (spec.type === 'theme' && input && document.activeElement !== input) {
      const html = themeOptions(spec, valueOf(spec.key)); // a custom theme added or renamed in the list below
      if (input._options !== html) { input.innerHTML = html; input._options = html; }
    }
    if (!input || document.activeElement === input || bad) return;
    const value = valueOf(spec.key);
    if (spec.type === 'hours') {
      const unitBox = row.querySelector('select[data-unit]');
      if (document.activeElement === unitBox) return;
      const typed = readHours(input.value, unitBox.value, spec.max);
      if (typed.stored === value) return; // "14 days" stays as typed when it means the stored 2 weeks
      const { count, unit } = splitHours(value);
      input.value = count;
      unitBox.value = unit;
      return;
    }
    if (spec.type === 'boolean') input.checked = !!value;
    else if (isNumeric(spec)) { if (parse(spec, input.value, roundSeconds()) !== value) input.value = num(spec, value); }
    else if (input.value !== String(value ?? '')) input.value = value ?? '';
  });
  root.querySelectorAll('[data-action=settingsChoice]').forEach((b) => {
    b.setAttribute('aria-pressed', String(String(valueOf(b.dataset.key)) === b.dataset.arg));
  });
  root.querySelectorAll('.set-row[data-extra=speed-estimate] [data-estimate]').forEach((p) => { p.textContent = estimateNow(); });
  root.querySelectorAll('.set-row[data-extra=speed-mlab]').forEach((row) => { row.toggleAttribute('data-inactive', valueOf('speedtest.provider') !== 'mlab'); });
  root.querySelectorAll('.set-row[data-extra=layout]').forEach((row) => {
    row.querySelector('[data-layout]').textContent = layoutNow();
    row.classList.toggle('unsaved', LAYOUT_KEYS.some(inDraft));
    paintMeta(row.querySelector('[data-meta-layout]'), layoutMetaHtml());
  });
}
