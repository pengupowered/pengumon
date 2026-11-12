// The Export view of Settings and the path editor, and the question before a draft is replaced.

import { escapeHtml } from './escape.js';
import { copyText } from './clipboard.js';

// The body of an Export view (Settings, the path editor): the YAML in a textarea, Copy, and a download link. Chrome blocks or
// warns about a file downloaded over plain HTTP from another machine ("insecure download"), whatever the page does;
// Copy works on every connection. `prefix` names the ids (<prefix>ExportText, <prefix>ExportNote) and the Copy action
// (<prefix>ExportCopy), which the dialog registers to call copyExport(prefix).
function exportViewHtml({ prefix, intro, text, href, file }) {
  return `<h3 class="section-t">Export</h3>
    <p class="fine top">${escapeHtml(intro)}</p>
    <textarea class="input export-text" id="${prefix}ExportText" rows="12" readonly spellcheck="false" aria-label="Exported YAML">${escapeHtml(text)}</textarea>
    <p class="export-b"><button type="button" class="btn sm" data-action="${prefix}ExportCopy"><svg class="i" aria-hidden="true"><use href="#i-copy"/></svg>Copy</button>
      <a class="btn sm" href="${href}" download="${file}"><svg class="i" aria-hidden="true"><use href="#i-download"/></svg>Download file</a>
      <span class="fine" id="${prefix}ExportNote" role="status"></span></p>
    ${window.isSecureContext ? '' : '<p class="fine">This page is served over plain HTTP, so Chrome may block the download as insecure. Copy works on every connection.</p>'}`;
}

// Shows or hides a dialog's Export view (button #<prefix>Export, section #<prefix>ExportView).
export function showExport(prefix, on) {
  const view = document.getElementById(`${prefix}ExportView`);
  document.getElementById(`${prefix}Export`).setAttribute('aria-expanded', String(on));
  view.hidden = !on;
  if (!on) view.innerHTML = '';
}

// Brings a panel at the top of a dialog's scrolling body into view and gives it the focus, wherever the body was scrolled to (opened
// from the footer while scrolled down, it would appear out of sight, and the button seem to do nothing).
export function reveal(box) {
  box.scrollIntoView({ block: 'start' });
  box.focus({ preventScroll: true });
}

// The Export button: opens the view with the YAML of `url` (Loading... first), or closes it. `what` names the thing in the
// error ("The path could not be exported (500)"). The view is brought into view and gets the focus, as the dialog's body
// scrolls under its header.
export async function exportToggle(prefix, { url, what, intro, file }) {
  const box = document.getElementById(`${prefix}ExportView`);
  const on = box.hidden;
  showExport(prefix, on);
  if (!on) return;
  box.innerHTML = '<p class="fine">Loading…</p>';
  reveal(box);
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`The ${what} could not be exported (${res.status}).`);
    box.innerHTML = exportViewHtml({ prefix, intro, text: await res.text(), href: url, file });
  } catch (e) {
    box.innerHTML = `<p class="fine">${escapeHtml(e.message)}</p>`;
  }
  reveal(box);
}

// Loading something over a draft (an earlier version, a file) throws the draft's edits away: with edits (`dirty`) the dialog
// asks first, through `ask(text, yesLabel, replace)`; without, `replace()` runs at once. Settings and the path editor ask
// in their own way, with the same words.
export function confirmReplaceDraft(dirty, text, replace, ask) {
  if (!dirty) { replace(); return; }
  ask(text, 'Replace the draft', replace);
}

export async function copyExport(prefix) {
  const area = document.getElementById(`${prefix}ExportText`);
  const copied = await copyText(area.value);
  document.getElementById(`${prefix}ExportNote`).textContent = copied ? 'Copied.' : 'Select the text and copy it.';
  if (!copied) area.select();
}
