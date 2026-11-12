// The ISP outage dossier: the evidence report for the provider (GET /api/incidents/dossier), copied as Markdown or
// downloaded. Opened from the header, the incident log and the reliability summary.

import { registerActions } from '../../js/dispatch.js';
import { copyText } from '../../js/clipboard.js';
import { escapeHtml } from '../../js/escape.js';
import { getTargetInfo } from '../../js/targets.js';
import { keepKeyboard, openModal } from '../../js/modal.js';
import { slug } from '../../js/format.js';
import { zoneParams } from '../../js/clock.js';
import { renderMarkdown } from './markdown.js';

// The ISP's name from config (line.isp_name); the dossier title and download name follow it.
export function ispSlug() {
  return slug(ispName || 'isp') || 'isp';
}

export function applyDossierTitle() {
  const title = document.getElementById('dossierTitleText');
  if (title) title.textContent = `${ispName || 'ISP'} Outage & Escalation Dossier`;
}

let ispName = null; // line.isp_name from /api/info; the title and the download name follow it

const readIspName = () => fetch('/api/info').then((res) => res.json()).then((info) => { ispName = info.isp_name || ''; })
  .catch(() => { if (ispName === null) ispName = ''; });

export async function openDossierModal() {
  if (ispName === null) await readIspName(); // the first time the title waits for the name; later opens show the last one
  applyDossierTitle();
  setDossierView('doc');
  openModal('dossierModal');
  document.querySelector('#dossierModal .m-close').focus(); // keyboard users land inside the dialog
  fetchDossier();
  readIspName().then(applyDossierTitle); // the name may have been changed in Settings since the last open
}

let latestDossierMarkdown = '';
let loadedHours = 24;   // the window latestDossierMarkdown was made for: the download is named after it
let seq = 0;            // the newest request; an older answer is dropped

function busy(on) {
  const doc = document.getElementById('dossierDoc');
  if (doc) doc.setAttribute('aria-busy', String(on));
  const status = document.getElementById('dossierStatus');
  if (status) status.textContent = on ? '' : 'Dossier ready';
  document.querySelectorAll('#btnCopyDossier, #btnDownloadDossier').forEach((b) => { b.disabled = on; });
}

const hoursText = (h) => (h % 24 === 0 && h >= 48 ? `${h / 24} days` : `${h} hours`);
const duration = (s) => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, '0')}s` : `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, '0')}m`);
const stat = (label, value, sub = '', text = false) => `<div class="stat"><span class="lbl">${escapeHtml(label)}</span><span class="val num${text ? ' txt' : ''}">${escapeHtml(value)}</span>${sub ? `<span class="sub">${escapeHtml(sub)}</span>` : ''}</div>`;

// The top of the document, from the structured figures the endpoint sends beside the Markdown: the counts, and where
// loss starts as the hops in order (the first hop with elevated loss and everything after it marked).
function summaryMarkup(data) {
  const stats = data.hop_stats || [];
  const first = stats.find((h) => h.target_id === data.primary_failing_hop);
  const firstIndex = first ? stats.indexOf(first) : -1;
  const tiles = [
    stat('Incidents (not expected)', String(data.total_incidents ?? 0)),
    stat('Total downtime', duration(data.total_duration_seconds || 0)),
    stat('Expected, left out', String(data.excluded_expected ?? 0), 'busy-line events'),
    stat('First hop with loss', first ? getTargetInfo(first.target_id).name : 'none isolated', first ? `${String(first.loss_pct)}% lost` : '', true)
  ].join('');
  const path = stats.length
    ? `<h4>Where loss starts</h4><div class="doc-path" role="list">${stats.map((h, i) => `<span class="dp${firstIndex >= 0 && i >= firstIndex ? ' bad' : ''}" role="listitem">${escapeHtml(getTargetInfo(h.target_id).name)}<small>${escapeHtml(String(h.loss_pct))}%</small></span>${i < stats.length - 1 ? '<i></i>' : ''}`).join('')}</div>` : '';
  return `<div class="doc-stats">${tiles}</div>${path}`;
}

function renderDocument(data) {
  const doc = renderMarkdown(data.formatted_markdown || '');
  document.getElementById('dossierDoc').innerHTML = `<header>${doc.title ? `<h3>${doc.title}</h3>` : ''}<p class="doc-meta">Evidence to send to your provider.</p>${doc.meta}</header>${summaryMarkup(data)}${doc.body}`;
}

// One request at a time: the server builds one dossier at a time (429 for another asker), so a window chosen while
// one is on its way waits for that answer, and only the newest wanted window is then asked for.
let running = false;
let wanted = false;

export async function fetchDossier() {
  const select = document.getElementById('dossierWindowSelect');
  if (running) {
    wanted = true;
    latestDossierMarkdown = '';
    busy(true);
    document.getElementById('dossierDoc').innerHTML = `<p class="muted">Compiling the evidence for the past ${escapeHtml(hoursText(Number(select ? select.value : 24)))}...</p>`;
    return;
  }
  running = true;
  try {
    do {
      wanted = false;
      await loadDossier();
    } while (wanted);
  } finally {
    running = false;
  }
}

async function loadDossier() {
  const select = document.getElementById('dossierWindowSelect');
  const hours = Number(select ? select.value : 24);
  const mine = ++seq;
  latestDossierMarkdown = ''; // nothing to copy or download until this window's answer is in
  busy(true);
  const out = document.getElementById('dossierOutput');
  const docEl = document.getElementById('dossierDoc');
  docEl.innerHTML = `<p class="muted">Compiling the evidence for the past ${escapeHtml(hoursText(hours))}...</p>`;
  out.textContent = '';

  try {
    const res = await fetch(`/api/incidents/dossier?hours=${hours}&${zoneParams()}`);
    if (res.status === 429) throw new Error('Another dossier is being built; try again in a few seconds');
    if (!res.ok) throw new Error('Failed to generate dossier');
    const data = await res.json();
    if (mine !== seq || wanted) return; // an older answer, or another window was chosen meanwhile: it is asked for next
    loadedHours = hours;
    latestDossierMarkdown = data.formatted_markdown || '';
    out.textContent = latestDossierMarkdown;
    renderDocument(data);
    busy(false);
  } catch (err) {
    if (mine !== seq || wanted) return;
    latestDossierMarkdown = '';
    docEl.innerHTML = `<p class="ink-crit">Error generating dossier: ${escapeHtml(err.message)}</p>`;
    busy(false);
  }
}

// Document (the report as a page) or Markdown (the exact text copied and downloaded).
export function setDossierView(view) {
  const md = view === 'md';
  document.getElementById('dossierDoc').hidden = md;
  document.getElementById('dossierOutput').hidden = !md;
  document.querySelectorAll('#dossierViews button').forEach((b) => {
    const on = b.dataset.arg === (md ? 'md' : 'doc');
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

export function copyDossierToClipboard() {
  if (!latestDossierMarkdown) return;
  copyText(latestDossierMarkdown).then((copied) => {
    if (!copied) { prompt('Copy Dossier Markdown:', latestDossierMarkdown); return; }
    const label = document.querySelector('#btnCopyDossier span');
    if (label) {
      label.textContent = 'Copied';
      setTimeout(() => { label.textContent = 'Copy as Markdown'; }, 2000);
    }
  });
}

export function downloadDossierMarkdown() {
  if (!latestDossierMarkdown) return;
  const blob = new Blob([latestDossierMarkdown], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${ispSlug()}_outage_dossier_${loadedHours}h.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// Escape and Tab (the dossier may sit on top of the incident inspector, which stays open beneath it).
keepKeyboard(document.getElementById('dossierModal'));

registerActions('click', {
  openDossierModal: () => openDossierModal(),
  copyDossierToClipboard: () => copyDossierToClipboard(),
  downloadDossierMarkdown: () => downloadDossierMarkdown(),
  setDossierView: (el) => setDossierView(el.dataset.arg)
});
registerActions('change', {
  fetchDossier: () => fetchDossier()
});
