// Add target: a new probe target (POST /api/targets), from the add-target dialog's form. The same rules as the server's
// (models.TargetConfig) are checked while typing, so a mistake is named next to its field; the server's own answer
// (a taken id, the limit of custom targets, a host it refuses) is shown the same way.

import { registerActions } from '../../js/dispatch.js';
import { closeModal, keepKeyboard } from '../../js/modal.js';
import { escapeHtml } from '../../js/escape.js';
import { hostProblem } from '../../js/host.js';
import { icon } from '../../js/dom.js';
import { initTargets } from '../../js/targets.js';
import { slug } from '../../js/format.js';

const ID_RE = /^[a-zA-Z0-9_-]{1,64}$/;
const FIELDS = ['name', 'address', 'id'];
const INPUT = { name: 'targetName', address: 'targetAddress', id: 'targetId' };
const HELP = {
  name: 'What the dashboard calls it.',
  address: 'An IPv4 or IPv6 address, or a host name.',
  id: 'Letters, digits, - and _ (for example game_server_east). Filled in from the name.'
};
const touched = new Set();
let autoId = ''; // the id last filled in from the name; an id typed by hand is never overwritten

const $ = (id) => document.getElementById(id);
const value = (field) => $(INPUT[field]).value.trim();

// The message for a value the server would refuse, or '' when it would take it.
export function problem(field, v) {
  if (field === 'name') return v ? '' : 'Give it a name.';
  if (field === 'id') {
    if (!v) return 'An id is needed.';
    return ID_RE.test(v) ? '' : 'Use 1 to 64 letters, digits, hyphens or underscores.';
  }
  return hostProblem(v);
}

function show(field, message) {
  const wrap = document.querySelector(`#addTargetForm [data-field=${field}]`);
  const input = $(INPUT[field]);
  const hint = $(`${INPUT[field]}Msg`);
  wrap.classList.toggle('invalid', !!message);
  wrap.classList.toggle('valid', !message && touched.has(field) && !!value(field));
  if (message) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
  hint.textContent = message || HELP[field];
}

function check(field) {
  const message = problem(field, value(field));
  show(field, touched.has(field) ? message : '');
  return message;
}

function banner(html) {
  const box = $('addTargetError');
  box.hidden = !html;
  box.innerHTML = html;
}

function clear() {
  $('addTargetForm').reset();
  touched.clear();
  autoId = '';
  banner('');
  FIELDS.forEach((f) => show(f, ''));
}

export function cancelAddTarget() {
  clear();
  closeModal('addTargetModal');
}

function onInput(el) {
  if (el.id === INPUT.name) {
    const id = $(INPUT.id);
    if (!id.value || id.value === autoId) { autoId = slug(el.value).slice(0, 64); id.value = autoId; if (touched.has('id')) check('id'); }
  } else if (el.id === INPUT.id) {
    touched.add('id');
  }
  const field = FIELDS.find((f) => INPUT[f] === el.id);
  if (field && touched.has(field)) check(field);
}

// The server's refusal, by field where it names one. FastAPI sends a string, or a list of {loc, msg}.
function serverProblems(detail) {
  const found = {};
  const rest = [];
  if (Array.isArray(detail)) {
    detail.forEach((d) => {
      const field = FIELDS.find((f) => (d.loc || []).includes(f));
      const msg = String(d.msg || '').replace(/^Value error, /, '');
      if (field) found[field] = msg; else rest.push(msg);
    });
  } else if (/id already exists/i.test(String(detail))) {
    found.id = 'That id is taken. Pick another.';
  } else {
    rest.push(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return { found, rest };
}

export async function handleAddTarget(e) {
  e.preventDefault();
  banner('');
  FIELDS.forEach((f) => touched.add(f));
  const bad = FIELDS.filter((f) => check(f));
  if (bad.length) { $(INPUT[bad[0]]).focus(); return; }
  const button = $('btnAddTarget');
  const label = button.querySelector('span');
  button.disabled = true;
  label.textContent = 'Adding…';
  try {
    const res = await fetch('/api/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: value('id'), name: value('name'), address: value('address'), category: $('targetCategory').value, family: Number($('targetFamily').value), type: 'icmp', enabled: true })
    });
    if (res.ok) {
      await initTargets(); // the new name and color for the matrix and the charts
      clear();
      closeModal('addTargetModal');
    } else {
      const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      const { found, rest } = serverProblems(err.detail);
      Object.entries(found).forEach(([field, msg]) => show(field, msg));
      if (rest.length) banner(`${icon('warn')}<div><b>The target was not added</b>${rest.map(escapeHtml).join(' ')}</div>`);
      const first = FIELDS.find((f) => found[f]);
      if (first) $(INPUT[first]).focus();
    }
  } catch (err) {
    banner(`${icon('warn')}<div><b>The target was not added</b>${escapeHtml(err.message)}</div>`);
  } finally {
    button.disabled = false;
    label.textContent = 'Add target';
  }
}

registerActions('submit', {
  addTarget: (el, ev) => handleAddTarget(ev)
});
registerActions('input', {
  validateTargetField: (el) => onInput(el)
});
registerActions('change', {
  touchTargetField: (el) => {
    const field = FIELDS.find((f) => INPUT[f] === el.id);
    if (field) { touched.add(field); check(field); }
  }
});
registerActions('click', {
  cancelAddTarget: () => cancelAddTarget()
});
keepKeyboard($('addTargetModal'), '#targetName', { onEscape: cancelAddTarget }); // Escape clears the form like Cancel
