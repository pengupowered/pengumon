// Ask AI: the troubleshooting pack (GET /api/ai-pack) as Markdown, previewed exactly as it is copied or downloaded.
// Opened from the header, the incident inspector (with that incident) and the dossier. The dialog starts at Full.
//
// The request and the answer (api/reports.py; this file is the only place that names them):
//   GET /api/ai-pack?scope=window&hours=1|24|168 | scope=incident&incident_id=<id>  &level=full|vague|anonymized
//       &detail=short|detailed  &tz_offset_minutes=<minutes east of UTC>  &tz=<the browser's IANA zone>
//   -> { markdown, roles: [{role, name}], modules: [{id, title, chars, budget_chars, trimmed}], tokens_estimate?, counts? }
//   422/404: the choice is wrong; 429: another build runs (Retry-After); 503: no repository. The route allows 6 a minute, so
//   a change waits for DEBOUNCE_MS of quiet before it asks.

import { registerActions } from '../../js/dispatch.js';
import { copyText } from '../../js/clipboard.js';
import { keepKeyboard, openModal } from '../../js/modal.js';
import { zoneParams } from '../../js/clock.js';

const AI_PACK_URL = '/api/ai-pack';
const DEBOUNCE_MS = 600;
const HOURS = { '1h': 1, '24h': 24, '7d': 168 };
const REFUSALS = { 404: 'that incident no longer exists', 422: 'those choices do not go together', 503: 'Pengumon has no stored history yet' };
const $ = (id) => document.getElementById(id);

let incidentId = null; // set when opened from the inspector
let shown = null; // the choices the text in the preview was made for, or null before the first answer
let wanted = null; // the choices now selected
let seq = 0; // each request has a number; an older answer than the latest one is dropped
let inflight = null;
let timer = null; // the debounce, and the retry after a 429

const choices = () => ({
  scope: $('aiPackWindow').value,
  form: $('aiPackForm').value,
  level: document.querySelector('input[name=aiPackLevel]:checked').value
});
const same = (a, b) => !!a && !!b && a.scope === b.scope && a.form === b.form && a.level === b.level;
const tokensText = (chars, est) => { const t = est || Math.ceil(chars / 4); return t >= 1000 ? `${(t / 1000).toFixed(1)}k` : String(t); };

function say(text, bad = false) {
  const note = $('aiPackNote');
  note.textContent = text;
  note.classList.toggle('bad', bad);
}

// Plain HTTP on the LAN: Chrome may block a download as insecure; Copy works on every connection (0.21's note).
const HTTP_NOTE = 'This page is served over plain HTTP, so Chrome may block the download as insecure. Copy works on every connection.';
const idleNote = () => (window.isSecureContext ? '' : HTTP_NOTE);

function setBusy(busy) {
  $('aiPackText').setAttribute('aria-busy', String(busy));
  // an empty box while the first answer is on its way says so (placeholder text is not part of what Copy gives)
  $('aiPackText').placeholder = busy && !$('aiPackText').value ? 'Building the pack...' : '';
  const ready = !busy && same(shown, wanted) && !!$('aiPackText').value;
  $('aiPackCopy').disabled = !ready;
  $('aiPackDownload').disabled = !ready;
}

function renderRoles(roles) {
  const list = $('aiPackWhoList');
  list.textContent = '';
  const rows = Array.isArray(roles) ? roles : Object.entries(roles || {}).map(([role, name]) => ({ role, name }));
  for (const r of rows) {
    const dt = document.createElement('dt');
    dt.textContent = r.role;
    const dd = document.createElement('dd');
    dd.textContent = r.name;
    list.append(dt, dd);
  }
  if (!rows.length) {
    const dd = document.createElement('dd');
    dd.textContent = 'No names were replaced at this level.';
    list.append(dd);
  }
}

function renderAnswer(data, made) {
  const md = data.markdown || '';
  $('aiPackText').value = md;
  $('aiPackText').scrollTop = 0;
  shown = made;
  $('aiPackSize').textContent = `about ${tokensText(md.length, data.tokens_estimate)} tokens (${md.length.toLocaleString('en-US')} characters)`;
  const counts = typeof data.counts === 'string' ? data.counts
    : data.counts ? Object.entries(data.counts).map(([k, n]) => `${n} ${k}`).join(', ') : '';
  const c = $('aiPackCounts');
  c.hidden = !counts;
  c.textContent = counts ? `Replaced in the text: ${counts}.` : '';
  const mods = (data.modules || []).map((m) => `${m.title}${m.trimmed ? ' (shortened)' : ''}`);
  const inc = $('aiPackIncludes');
  inc.hidden = !mods.length;
  inc.textContent = mods.length ? `Includes: ${mods.join(', ')}.` : '';
  renderRoles(data.roles);
}

// The viewer's IANA zone (America/New_York): the pack's times follow its daylight-saving changes when Settings names no
// zone. Empty where the browser does not say.
function query(c) {
  const p = new URLSearchParams(c.scope === 'incident' ? { scope: 'incident', incident_id: String(incidentId) } : { scope: 'window', hours: String(HOURS[c.scope]) });
  p.set('level', c.level);
  p.set('detail', c.form);
  zoneParams().forEach((value, key) => p.set(key, value));
  return p;
}

// Builds the pack for the selected choices. The previous preview stays on screen (dimmed, Copy and Download off, so a text
// made for other choices is never copied) until the new one arrives; a failure leaves it and says so in the footer.
export async function fetchAiPack() {
  clearTimeout(timer);
  if (!$('aiPackModal').classList.contains('active')) return;
  const made = choices();
  wanted = made;
  const mine = ++seq;
  if (inflight) inflight.abort();
  inflight = new AbortController();
  setBusy(true);
  say(shown ? 'Updating the preview...' : 'Building the pack...');
  try {
    const res = await fetch(`${AI_PACK_URL}?${query(made)}`, { signal: inflight.signal });
    if (res.status === 429) {
      const wait = Math.min(30, Math.max(2, Number(res.headers.get('Retry-After')) || 5));
      say(`Busy, retrying in ${wait} s. The text above is still the earlier choice, so Copy and Download are off.`);
      timer = setTimeout(fetchAiPack, wait * 1000);
      return;
    }
    if (!res.ok) throw new Error(REFUSALS[res.status] || `HTTP ${res.status}`);
    const data = await res.json();
    if (mine !== seq) return;
    renderAnswer(data, made);
    setBusy(false);
    say(idleNote());
  } catch (err) {
    if (mine !== seq || err.name === 'AbortError') return;
    setBusy(false);
    say(shown ? `Could not update the preview (${err.message}). The text above is still the earlier choice, so Copy and Download are off. Change a choice to try again.`
      : `Could not build the pack (${err.message}). Change a choice to try again.`, true);
  }
}

// A change of choice: the preview waits (Copy and Download off at once), then one request for the last choice made.
function choiceChanged() {
  wanted = choices();
  setBusy(true);
  say(shown ? 'Updating the preview...' : 'Building the pack...');
  clearTimeout(timer);
  timer = setTimeout(fetchAiPack, DEBOUNCE_MS);
}

// `el.dataset.arg`: the incident id to cover, or nothing for a window. Each opening starts at Full, Short and the past 24 hours.
export function openAiPackModal(el) {
  const id = el && el.dataset ? Number(el.dataset.arg) : NaN;
  incidentId = Number.isFinite(id) && el.dataset.arg !== '' ? id : null;
  const option = $('aiPackIncidentOption');
  option.hidden = option.disabled = incidentId === null;
  $('aiPackWindow').value = incidentId === null ? '24h' : 'incident';
  $('aiPackForm').value = 'short';
  document.querySelector('input[name=aiPackLevel][value=full]').checked = true;
  $('aiPackWho').open = false;
  $('aiPackBody').scrollTop = 0;
  clearTimeout(timer);
  shown = null;
  $('aiPackText').value = '';
  $('aiPackSize').textContent = '';
  say('');
  openModal('aiPackModal');
  fetchAiPack();
}

export async function copyAiPack() {
  const area = $('aiPackText');
  if (!area.value || !same(shown, wanted)) return;
  const copied = await copyText(area.value);
  if (!copied) { area.focus(); area.select(); }
  say(copied ? `Copied ${tokensText(area.value.length)} tokens. Paste it into the chat.` : 'The browser would not copy. The text is selected: press Ctrl+C (Cmd+C).', !copied);
}

export function downloadAiPack() {
  const text = $('aiPackText').value;
  if (!text || !same(shown, wanted)) return;
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `pengumon_ai_pack_${shown.scope}_${shown.level}_${shown.form}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  say(window.isSecureContext ? 'Saved the file.' : `Asked the browser to save the file. ${HTTP_NOTE}`);
}

// Escape and Tab (the dialog may sit on top of the incident inspector or the dossier).
keepKeyboard($('aiPackModal'), '.m-close');

registerActions('click', {
  openAiPackModal: (el) => openAiPackModal(el),
  aiPackCopy: () => copyAiPack(),
  aiPackDownload: () => downloadAiPack()
});
registerActions('change', {
  aiPackChange: () => choiceChanged()
});
