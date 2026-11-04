// One setting as a row: label, input, unit, and under it the effect, the default, the badges and "Reset". The inputs
// are built from the schema (type, range, choices); a value is shown in the schema's unit and stored in the server's.

import { escapeHtml } from '../../js/util.js';
import { fmt, isNumeric, parse, problem, same, toShown } from './model.js';
import { S, isEnv, inDraft, roundSeconds, setValue, valueOf, valueState } from './store.js';

const inputId = (section, key) => `set-in-${section}-${key.replace(/[^a-z0-9]+/gi, '-')}`;
const num = (spec, stored) => (stored === null || stored === undefined ? '' : String(toShown(spec, stored, roundSeconds())));

// The placeholder of an empty box: the ISP-hop level says what it stands for (design 6.2).
function placeholderFor(spec) {
  return spec.key === 'classifier.isp_latency_spike_threshold_ms' ? 'same as internet' : 'not set';
}

// "same as internet (80 ms)": what the empty ISP-hop level stands for, with the Internet level it follows now.
function ispDefault() {
  const pub = S.spec['classifier.public_latency_spike_threshold_ms'];
  return `same as internet (${fmt(pub, valueOf(pub.key), roundSeconds())})`;
}

function zones(current) {
  let all = [];
  try { all = Intl.supportedValuesOf('timeZone'); } catch (e) { all = []; }
  let mine = '';
  try { mine = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { mine = ''; }
  const first = [mine, current].filter((z, i, a) => z && a.indexOf(z) === i);
  return [...first, ...all.filter((z) => !first.includes(z))];
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
        `<button type="button" data-action="settingsChoice" data-key="${escapeHtml(spec.key)}" data-arg="${escapeHtml(c)}" aria-pressed="${c === value}"${env ? ' disabled' : ''}>${escapeHtml(c)}</button>`).join('')}</span>`;
    }
    return `<select class="input" ${common}${env ? ' disabled' : ''}>${choices.map((c) => `<option value="${escapeHtml(c)}"${c === value ? ' selected' : ''}>${escapeHtml(c)}</option>`).join('')}</select>`;
  }
  if (spec.type === 'timezone') {
    return `<select class="input" ${common}${env ? ' disabled' : ''}>${zones(value).map((z) => `<option value="${escapeHtml(z)}"${z === value ? ' selected' : ''}>${escapeHtml(z)}</option>`).join('')}</select>`;
  }
  if (spec.type === 'time') {
    return `<input class="input" type="time" ${common}${locked} value="${escapeHtml(value || '')}">`;
  }
  if (spec.type === 'layout') {
    const list = Array.isArray(value) ? value : [];
    return `<span class="set-layout" id="${id}">${list.length ? escapeHtml(list.join(', ')) : 'the default'}</span>`;
  }
  if (isNumeric(spec)) {
    return `<input class="input num" type="text" inputmode="decimal" autocomplete="off" ${common}${locked} value="${escapeHtml(num(spec, value))}" placeholder="${escapeHtml(placeholderFor(spec))}" aria-describedby="${id}-m">`;
  }
  return `<input class="input" type="text" autocomplete="off" spellcheck="false" ${common}${locked} value="${escapeHtml(value ?? '')}"${spec.max_length ? ` maxlength="${spec.max_length}"` : ''} placeholder="not set" aria-describedby="${id}-m">`;
}

// "It is 21:40 there now.": the clock in the time zone that quiet hours are read in (UTC until one is picked).
function zoneClock() {
  const zone = String(valueOf('general.timezone') || '') || 'UTC';
  try {
    const time = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: zone }).format(new Date());
    return `<span class="set-now" data-now>It is ${escapeHtml(time)} there now (${escapeHtml(zone)}).</span>`;
  } catch (e) { return ''; }
}

// The line under the input: effect, default, badges, Reset. Rebuilt after every edit; the input itself is not touched.
export function metaHtml(spec) {
  const state = valueState(spec.key);
  const env = isEnv(spec.key);
  const unsaved = inDraft(spec.key);
  const stored = unsaved ? S.draft.values[spec.key] !== null : (state.stored ?? null) !== null;
  // a stored value can equal the default (the import keeps every value config.yaml set): not a change, so no badge and no Reset
  const differs = stored && !same(unsaved ? S.draft.values[spec.key] : state.stored, spec.default);
  const chips = [];
  if (env) chips.push(`<span class="chip st st-unknown" data-badge="env">From environment · ${escapeHtml(spec.env || '')}</span>`);
  else {
    if (differs) chips.push('<span class="chip st st-busy" data-badge="changed">Changed</span>');
    if (unsaved) chips.push('<span class="chip st st-warn" data-badge="unsaved">Unsaved</span>');
  }
  if (state.pending_restart) chips.push('<span class="chip st st-warn" data-badge="restart">Applies after a restart</span>');
  if (state.live_failed) chips.push('<span class="chip st st-crit" data-badge="live-failed">Saved; applies at the next start</span>');
  const reset = !env && differs // a value equal to the default (stored or not) has nothing to reset
    ? `<button type="button" class="set-lnk" data-action="settingsReset" data-key="${escapeHtml(spec.key)}" data-needs-edit>Reset to default</button>` : '';
  const def = `Default ${escapeHtml(spec.key === 'classifier.isp_latency_spike_threshold_ms' ? ispDefault() : fmt(spec, spec.default, roundSeconds()))}`;
  const warn = S.warnings.filter((w) => w.where === `values.${spec.key}`).map((w) => `<span class="set-warn">${escapeHtml(w.message)}</span>`).join('');
  const clock = spec.key === 'alerts.quiet_hours.start' || spec.key === 'alerts.quiet_hours.end' ? zoneClock() : '';
  return `<span class="set-effect">${escapeHtml(spec.effect)}</span> <span class="set-def">${def}</span> ${chips.join(' ')} ${reset}${warn} ${clock}`;
}

function relatedHtml(spec) {
  const other = spec.related && S.spec[spec.related];
  if (!other) return '';
  return `<span class="set-rel">incident rule: <b data-rel="${escapeHtml(other.key)}">${escapeHtml(fmt(other, valueOf(other.key), roundSeconds()))}</b>
    <button type="button" class="set-lnk" data-action="settingsJump" data-arg="${escapeHtml(other.key)}">Show</button></span>`;
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

// The input of `el` read as a stored value: {stored} or {problem}.
export function readInput(el, spec) {
  if (spec.type === 'boolean') return { stored: el.checked };
  const text = el.value;
  const why = problem(spec, text, roundSeconds());
  if (why) return { problem: why };
  return { stored: spec.type === 'timezone' || spec.type === 'choice' ? text : parse(spec, text, roundSeconds()) };
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
  if (!isEnv(key)) setValue(key, choice);
}

// Redraws a meta line only when its content changed, and keeps focus on its "Reset to default" if that had it: a redraw
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
    if (rel) rel.textContent = fmt(S.spec[rel.dataset.rel], valueOf(rel.dataset.rel), roundSeconds());
    if (!input || document.activeElement === input || bad) return;
    const value = valueOf(spec.key);
    if (spec.type === 'boolean') input.checked = !!value;
    else if (isNumeric(spec)) { if (parse(spec, input.value, roundSeconds()) !== value) input.value = num(spec, value); input.placeholder = placeholderFor(spec); }
    else if (input.value !== String(value ?? '')) input.value = value ?? '';
  });
  root.querySelectorAll('[data-action=settingsChoice]').forEach((b) => {
    b.setAttribute('aria-pressed', String(valueOf(b.dataset.key) === b.dataset.arg));
  });
  root.querySelectorAll('.set-layout').forEach((lay) => {
    const key = lay.closest('.set-row').dataset.key;
    const value = valueOf(key);
    lay.textContent = Array.isArray(value) && value.length ? value.join(', ') : 'the default';
  });
}
