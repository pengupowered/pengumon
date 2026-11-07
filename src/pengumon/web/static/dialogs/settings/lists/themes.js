// Themes (display.themes holds the custom ones): the built-in themes with Duplicate and Export, the custom themes as
// cards whose colours can be changed, Import of a theme file and Export of any theme as one (docs/themes.md, "Custom
// themes"). A custom theme has colours only (and the text sizes and corner radii a file brings): no shapes, no effect.
// Like every list it is a draft until Save; the server checks each theme (pengumon.web.themes, custom rules).

import { registerActions } from '../../../js/dispatch.js';
import { copyText, escapeHtml, icon } from '../../../js/util.js';
import { loadThemes } from '../../../js/themes.js';
import { clone } from '../model.js';

const MODES = ['dark', 'light'];
const SIZES = ['--fs-xs', '--fs-sm', '--fs-md', '--fs-lg', '--fs-xl', '--fs-2xl', '--r-xs', '--r-sm', '--r-md', '--r-lg', '--r-pill'];
// the colours in plain words, grouped as the editor shows them (the token name is the field's tooltip)
const GROUPS = [
  ['Backgrounds', [['--bg', 'Page'], ['--surface-1', 'Cards'], ['--surface-2', 'Insets'], ['--surface-3', 'Hover and chips'],
    ['--line', 'Hairlines'], ['--line-strong', 'Borders'], ['--scrim', 'Behind dialogs']]],
  ['Text', [['--ink-1', 'Main'], ['--ink-2', 'Secondary'], ['--ink-3', 'Muted'], ['--ink-4', 'Faint']]],
  ['Accent', [['--accent', 'Accent'], ['--accent-bg', 'Accent tint'], ['--on-accent', 'Text on accent'], ['--focus', 'Keyboard focus']]],
  ['Status', [['--good', 'Healthy'], ['--good-bg', 'Healthy tint'], ['--warn', 'Warning'], ['--warn-bg', 'Warning tint'],
    ['--warn-fill', 'Warning bars'], ['--serious', 'Serious'], ['--serious-bg', 'Serious tint'], ['--crit', 'Outage'],
    ['--crit-bg', 'Outage tint'], ['--busy', 'Expected'], ['--busy-bg', 'Expected tint'], ['--unknown', 'Unknown'],
    ['--unknown-bg', 'Unknown tint'], ['--nodata', 'No data']]],
  ['Chart lines', [1, 2, 3, 4, 5, 6, 7, 8].map((n) => [`--s${n}`, `Line ${n}`])],
  ['Loss heat', [['--q0', 'None'], ['--q1', 'Little'], ['--q2', 'Some'], ['--q3', 'Much'], ['--q4', 'Most']]]
];
const HEX6 = /^#[0-9a-f]{6}$/i;
const COLOUR = /^(#[0-9a-f]{3}|#[0-9a-f]{6}|#[0-9a-f]{8}|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*(0|1|0?\.\d+)\s*)?\))$/i;
const modeWords = (modes) => (modes.length === 2 ? 'dark and light' : `${modes[0]} only`);

let items = [];
let box = null;
let ctx = null;
let data = null; // GET /api/themes
let errors = {}; // item index -> message
let notes = []; // [{level, text}] the answer to an Import or a Duplicate
let shown = null; // {name, text}: the Export box
let exportUrl = '';
const open = new Set();
const wired = new WeakSet();

const defaults = () => data.themes.find((t) => t.id === 'default').theme.tokens;
const colourNames = () => Object.keys(defaults().dark);
const takenIds = () => [...data.themes.filter((t) => !t.custom).map((t) => t.id), ...items.map((t) => t.id)];
function newId(name) {
  const root = `custom-${String(name || 'theme').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'theme'}`.slice(0, 36);
  let id = root;
  for (let k = 2; takenIds().includes(id); k++) id = `${root}-${k}`;
  return id;
}

// A custom theme from any theme.json: its modes and colours (a missing colour comes from the standard theme), the text
// sizes and radii it sets; everything else (fonts, shapes, effects) stays behind. Says what it filled in or left out.
function fromTheme(raw, name) {
  if (!raw || typeof raw !== 'object') throw new Error('The file is not a theme (a JSON object).');
  const modes = MODES.filter((m) => Array.isArray(raw.modes) && raw.modes.includes(m));
  if (!modes.length) throw new Error('The file names no mode (dark or light).');
  const base = defaults();
  const tokens = {};
  let filled = 0;
  const dropped = new Set();
  modes.forEach((m) => {
    const given = raw.tokens && typeof raw.tokens[m] === 'object' && raw.tokens[m] ? raw.tokens[m] : {};
    tokens[m] = {};
    colourNames().forEach((n) => {
      if (typeof given[n] === 'string') tokens[m][n] = given[n].trim();
      else { tokens[m][n] = base[m][n]; filled++; }
    });
    SIZES.forEach((n) => { if (typeof given[n] === 'string') tokens[m][n] = given[n].trim(); });
    Object.keys(given).forEach((n) => { if (!colourNames().includes(n) && !SIZES.includes(n)) dropped.add(n); });
  });
  const theme = { id: newId(name), name: String(name).slice(0, 40), description: typeof raw.description === 'string' ? raw.description.slice(0, 200) : '', modes, tokens, tags: [] };
  return { theme, filled, dropped: [...dropped], effect: !!raw.effect };
}

const asFile = (t) => ({ id: t.id, name: t.name, description: t.description || '', modes: t.modes, tokens: t.tokens, tags: t.tags || [] });

// ---------- drawing ----------
function colourField(i, mode, name, label, value) {
  const id = `th-${i}-${mode}-${name.slice(2)}`;
  if (HEX6.test(defaults()[mode]?.[name] || defaults().dark[name])) {
    return `<label class="th-c" for="${id}" title="${escapeHtml(name)}"><input type="color" id="${id}" data-tc="${mode} ${name}" value="${escapeHtml(HEX6.test(value) ? value : '#000000')}"><span>${escapeHtml(label)}</span></label>`;
  }
  // a translucent colour (a tint, the dimming behind dialogs): its text, on a row of its own
  return `<label class="th-c th-text" for="${id}" title="${escapeHtml(name)}"><span>${escapeHtml(label)}</span><input class="input mono" type="text" id="${id}" data-tc="${mode} ${name}" value="${escapeHtml(value || '')}" spellcheck="false" autocomplete="off"></label>`;
}

function coloursHtml(item, i) {
  return item.modes.map((mode) => `<div class="th-mode"><h6 class="th-mode-t">${mode === 'dark' ? 'Dark' : 'Light'} colours</h6>
    ${GROUPS.map(([group, names]) => `<div class="th-group"><span class="th-group-t">${escapeHtml(group)}</span><div class="th-grid">
      ${names.map(([name, label]) => colourField(i, mode, name, label, item.tokens[mode]?.[name])).join('')}</div></div>`).join('')}</div>`).join('');
}

function cardHtml(item, i) {
  const bad = errors[i];
  return `<details class="set-card th-card" data-i="${i}" data-id="${escapeHtml(item.id)}"${open.has(item.id) ? ' open' : ''}>
    <summary>${icon('chevron', 'set-chev')}<span class="set-card-t">${escapeHtml(item.name || item.id)}</span><span class="set-card-s">${escapeHtml(modeWords(item.modes))}</span>${bad ? '<span class="chip st st-crit">Needs a fix</span>' : ''}</summary>
    <div class="set-card-b">
      <p class="set-msg th-msg" role="alert">${escapeHtml(bad || '')}</p>
      <div class="set-grid">
        <div class="set-f"><label for="th-${i}-name">Name</label><input class="input" type="text" id="th-${i}-name" data-tf="name" maxlength="40" value="${escapeHtml(item.name)}"></div>
        <div class="set-f"><label for="th-${i}-desc">Description <span class="muted">(optional)</span></label><input class="input" type="text" id="th-${i}-desc" data-tf="description" maxlength="200" value="${escapeHtml(item.description || '')}"></div>
      </div>
      ${coloursHtml(item, i)}
      <div class="set-card-f">
        <button type="button" class="btn sm" data-action="themesAct" data-ta="export" data-i="${i}">${icon('download')}Export</button>
        <button type="button" class="btn sm" data-action="themesAct" data-ta="duplicate" data-i="${i}" data-needs-edit>${icon('copy')}Duplicate</button>
        <button type="button" class="btn sm danger" data-action="themesAct" data-ta="remove" data-i="${i}" data-needs-edit>${icon('close')}Remove</button>
      </div>
    </div></details>`;
}

function builtinHtml(t) {
  return `<li class="th-row"><span class="th-name"><b>${escapeHtml(t.name)}</b><span>${escapeHtml(modeWords(t.modes))}${t.effect ? ' · animated effect' : ''}${t.skin ? ' · own shapes' : ''}</span></span>
    <span class="th-desc">${escapeHtml(t.description)}</span>
    <span class="th-acts"><button type="button" class="btn sm" data-action="themesAct" data-ta="export-builtin" data-arg="${escapeHtml(t.id)}" aria-label="Export ${escapeHtml(t.name)}">${icon('download')}Export</button>
    <button type="button" class="btn sm" data-action="themesAct" data-ta="duplicate-builtin" data-arg="${escapeHtml(t.id)}" data-needs-edit aria-label="Duplicate ${escapeHtml(t.name)}">${icon('copy')}Duplicate</button></span></li>`;
}

function exportHtml() {
  if (!shown) return '';
  if (exportUrl) URL.revokeObjectURL(exportUrl);
  exportUrl = URL.createObjectURL(new Blob([shown.text], { type: 'application/json' }));
  const file = `${shown.id}.theme.json`;
  return `<div class="th-export" id="themeExport" tabindex="-1"><h5 class="set-sub">Export: ${escapeHtml(shown.name)}</h5>
    <p class="fine top">This theme as a file. Import reads it on another install; theme authors start from it (docs/themes.md).</p>
    <textarea class="input export-text" id="themeExportText" rows="8" readonly spellcheck="false" aria-label="The theme file">${escapeHtml(shown.text)}</textarea>
    <p class="export-b"><button type="button" class="btn sm" data-action="themesAct" data-ta="copy">${icon('copy')}Copy</button>
      <a class="btn sm" href="${exportUrl}" download="${escapeHtml(file)}">${icon('download')}Download file</a>
      <button type="button" class="btn sm" data-action="themesAct" data-ta="close-export">Close</button>
      <span class="fine" id="themeExportNote" role="status"></span></p></div>`;
}

function draw() {
  if (!box) return;
  if (!data) { box.innerHTML = '<div class="set-list" data-list="themes"><h4 class="set-group-t">Themes</h4><p class="fine top">Loading…</p></div>'; return; }
  const builtins = data.themes.filter((t) => !t.custom);
  box.innerHTML = `<div class="set-list" data-list="themes">
    <div class="set-list-h"><h4 class="set-group-t">Themes</h4></div>
    <p class="fine top">Every browser can pick any of these in the header's theme menu. To make your own, duplicate a theme and change its colours, or import a theme file. Your own themes have colours only: the shapes and animated effects stay with the built-in ones.</p>
    <ul class="th-list">${builtins.map(builtinHtml).join('')}</ul>
    <h5 class="set-sub">Your themes</h5>
    ${notes.map((n) => `<div class="result st-${n.level}" role="status">${escapeHtml(n.text)}</div>`).join('')}
    ${items.length ? items.map(cardHtml).join('') : '<p class="fine top set-empty">None yet.</p>'}
    <div class="th-add"><button type="button" class="btn sm outline" data-action="themesAct" data-ta="import" data-needs-edit>${icon('plus')}<span>Import a theme file</span></button>
      <input type="file" id="themeImportFile" accept=".json,application/json" hidden></div>
    ${exportHtml()}</div>`;
}

// ---------- changes ----------
function changed() {
  ctx.markDirty();
}

function add(theme, note) {
  items.push(theme);
  open.add(theme.id);
  notes = [note];
  errors = {};
  changed();
  draw();
  box.querySelector(`.th-card[data-id="${CSS.escape(theme.id)}"] summary`)?.focus();
}

function duplicate(raw, name) {
  const made = fromTheme(raw, `${name} copy`.slice(0, 40));
  add(made.theme, { level: 'unknown', text: `${made.theme.name} is a draft: change its colours, then Save.` });
}

async function importFile(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  let raw;
  try {
    raw = JSON.parse(await file.text());
  } catch (e) {
    notes = [{ level: 'crit', text: `${file.name} is not a theme file: it is not JSON.` }];
    draw();
    return;
  }
  try {
    const made = fromTheme(raw, typeof raw.name === 'string' && raw.name.trim() ? raw.name.trim() : file.name.replace(/\.json$/i, ''));
    const left = [];
    if (made.filled) left.push(`${made.filled} missing colour${made.filled === 1 ? '' : 's'} come from the standard theme`);
    if (made.dropped.length || made.effect) left.push('its fonts, shapes and effects are left out (your themes have colours only)');
    add(made.theme, { level: left.length ? 'warn' : 'unknown', text: `Read ${file.name} as ${made.theme.name}${left.length ? `; ${left.join('; ')}` : ''}. It is a draft: Save to keep it.` });
  } catch (e) {
    notes = [{ level: 'crit', text: `${file.name} was not imported. ${e.message}` }];
    draw();
  }
}

function showExportOf(theme, name) {
  shown = { id: theme.id, name, text: JSON.stringify(theme, null, 2) };
  draw();
  const el = box.querySelector('#themeExport');
  el?.scrollIntoView({ block: 'nearest' });
  el?.focus({ preventScroll: true });
}

function act(b) {
  if (!box || !box.contains(b)) return;
  const i = Number(b.dataset.i);
  const what = b.dataset.ta;
  const builtin = data && data.themes.find((t) => t.id === b.dataset.arg);
  if (what === 'export') showExportOf(asFile(items[i]), items[i].name);
  else if (what === 'export-builtin' && builtin) showExportOf(builtin.theme, builtin.name);
  else if (what === 'copy') copyText(document.getElementById('themeExportText').value).then((done) => { document.getElementById('themeExportNote').textContent = done ? 'Copied' : 'Select the text and copy it'; });
  else if (what === 'close-export') { shown = null; draw(); }
  else if (!ctx.canEdit()) return;
  else if (what === 'import') document.getElementById('themeImportFile').click();
  else if (what === 'duplicate-builtin' && builtin) duplicate(builtin.theme, builtin.name);
  else if (what === 'duplicate') duplicate(asFile(items[i]), items[i].name);
  else if (what === 'remove') {
    const [gone] = items.splice(i, 1);
    open.delete(gone.id);
    errors = {};
    notes = [];
    changed();
    draw();
  }
}
registerActions('click', { themesAct: (el) => act(el) });

function wire() {
  if (wired.has(box)) return;
  wired.add(box);
  const edited = (ev) => {
    const el = ev.target instanceof Element ? ev.target : null;
    if (!el || !ctx.canEdit()) return;
    if (el.id === 'themeImportFile') { if (ev.type === 'change') importFile(el); return; }
    const card = el.closest('.th-card');
    if (!card) return;
    const item = items[Number(card.dataset.i)];
    if (el.dataset.tf) {
      item[el.dataset.tf] = el.value;
      if (el.dataset.tf === 'name') card.querySelector('.set-card-t').textContent = el.value || item.id;
    } else if (el.dataset.tc) {
      const [mode, name] = el.dataset.tc.split(' ');
      const value = el.value.trim();
      el.setAttribute('aria-invalid', String(!COLOUR.test(value)));
      item.tokens[mode][name] = value;
    } else return;
    changed();
  };
  box.addEventListener('input', edited);
  box.addEventListener('change', edited);
  box.addEventListener('toggle', (ev) => {
    const card = ev.target;
    if (!(card instanceof Element) || !card.classList.contains('th-card')) return;
    if (card.open) open.add(card.dataset.id); else open.delete(card.dataset.id);
  }, true);
}

export default {
  id: 'themes',
  render(container, its, cx) {
    box = container;
    ctx = cx;
    items = clone(its);
    errors = {};
    notes = [];
    shown = null;
    wire();
    draw();
    if (!data) loadThemes().then((d) => { data = d; draw(); }).catch(() => { box.innerHTML = '<p class="fine">The themes could not be read. Close Settings and open it again.</p>'; });
  },
  collect: () => clone(items),
  // the server's answers for this list, {"0.": "message", "0.tokens": "..."}: shown on the card, in place (a redraw would
  // take the focus from the colour being typed)
  problems(byWhere) {
    errors = {};
    Object.entries(byWhere || {}).forEach(([where, message]) => {
      const i = Number(where.split('.')[0]);
      if (Number.isInteger(i) && !(i in errors)) errors[i] = message;
    });
    if (!box) return;
    box.querySelectorAll('.th-card').forEach((card) => {
      const message = errors[card.dataset.i] || '';
      card.querySelector('.th-msg').textContent = message;
      const chip = card.querySelector('summary .st-crit');
      if (message && !chip) card.querySelector('summary').insertAdjacentHTML('beforeend', '<span class="chip st st-crit">Needs a fix</span>');
      if (!message && chip) chip.remove();
      if (message) { card.open = true; open.add(card.dataset.id); }
    });
  },
  redraw() { if (data) draw(); }
};
