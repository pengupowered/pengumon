// The disk table under Retention: what each kind of data holds now and when its history is full, as the "Keep for"
// numbers are edited (the numbers come from GET /api/storage; the arithmetic is in model.js), and the question that
// Save asks when a number was lowered (what it deletes). Shown only to someone who may edit: the storage figures are
// for the owner, as the settings are.

import { escapeHtml } from '../../js/escape.js';
import { bytesText, freedNow, storedText, whenFull } from './model.js';
import { S, valueOf, valueState } from './store.js';
import { U } from './ui.js';

const YEAR = 365;

// The kinds that have a setting, with the days the draft has and the days now saved.
function rows() {
  const kinds = (U.storage && U.storage.kinds) || [];
  return kinds.map((k) => ({ kind: k, days: k.setting ? valueOf(k.setting) : null, saved: k.setting ? valueState(k.setting).value : null }));
}

const fullText = (kind, days) => {
  const bytes = whenFull(kind, days);
  if (bytes === null) return `grows ~${bytesText(kind.per_day * YEAR)} a year`;
  return bytesText(bytes);
};

// "Change" of a row: what lowering deletes now, else the difference when full against what is saved.
function changeText(r) {
  const { kind, days, saved } = r;
  if (!kind.setting) return '—';
  const freed = freedNow(kind, days);
  if (freed > 0) return `frees ${bytesText(freed)}`;
  const was = whenFull(kind, saved);
  const will = whenFull(kind, days);
  if (was === null && will === null) return '—';
  if (will === null) return 'grows without end';
  if (was === null) return `${bytesText(will)} at most`;
  const delta = will - was;
  if (Math.abs(delta) < 0.05 * 1e6) return '—';
  return delta > 0 ? `+${bytesText(delta)}` : `frees ${bytesText(-delta)} when full`;
}

function keepText(r) {
  const { kind, days } = r;
  if (kind.id === 'full') return `${valueOf('database.retention_days')} / ${valueOf('database.slot_retention_days')} / ${valueOf('database.rollup_retention_days')} days, fixed`;
  if (!kind.setting) return '—';
  const base = days > 0 ? `${days} days` : 'forever';
  const stored = storedText(kind, days);
  return stored ? `${base} (${stored})` : base;
}

function rowHtml(r) {
  const { kind } = r;
  return `<tr data-kind="${escapeHtml(kind.id)}"><th scope="row">${escapeHtml(kind.label)}</th><td data-cell="keep">${escapeHtml(keepText(r))}</td><td data-cell="now">${escapeHtml(bytesText(kind.bytes))}</td><td data-cell="full">${escapeHtml(kind.setting ? fullText(kind, r.days) : bytesText(kind.fixed || kind.bytes))}</td><td data-cell="change">${escapeHtml(changeText(r))}</td></tr>`;
}

function totalHtml(list) {
  const now = list.reduce((n, r) => n + r.kind.bytes, 0);
  const forever = list.some((r) => r.kind.setting && whenFull(r.kind, r.days) === null);
  const full = list.reduce((n, r) => n + (r.kind.setting ? whenFull(r.kind, r.days) || 0 : r.kind.fixed || r.kind.bytes), 0);
  const freed = list.reduce((n, r) => n + freedNow(r.kind, r.days), 0);
  const fullText2 = `${bytesText(full)}${forever ? ' + incidents without end' : ''}`;
  return `<tr class="set-total" data-kind="total"><th scope="row">Total</th><td></td><td data-cell="now">${escapeHtml(bytesText(now))}</td><td data-cell="full">${escapeHtml(fullText2)}</td><td data-cell="change">${escapeHtml(freed > 0 ? `frees ${bytesText(freed)}` : '—')}</td></tr>`;
}

function diskText() {
  const s = U.storage;
  const free = s.disk && s.disk.free !== null && s.disk.free !== undefined ? ` · ${bytesText(s.disk.free)} free on its disk` : '';
  const wal = s.wal_bytes ? ` (log ${bytesText(s.wal_bytes)})` : '';
  return `Disk: ${bytesText(s.file_bytes)} now${wal}${free}${s.estimate ? ' · figures are estimates on this SQLite build' : ''}`;
}

function tableHtml() {
  if (U.storage === undefined) return '<p class="fine" data-storage-loading>Reading the disk figures…</p>';
  if (!U.storage) return '<p class="fine" data-storage-missing>The disk figures could not be read.</p>';
  const list = rows();
  return `<div class="set-storage-wrap"><table class="set-storage"><thead><tr><th scope="col">Kind</th><th scope="col">Keep</th><th scope="col">Now</th><th scope="col">When full</th><th scope="col">Change</th></tr></thead><tbody>${list.map(rowHtml).join('')}${totalHtml(list)}</tbody></table></div>
    <p class="set-estimate" data-storage-disk aria-live="polite">${escapeHtml(diskText())}</p>
    <p class="fine">At today's rate. Full detail (every second, 10 seconds, 1 minute) is kept ${escapeHtml(`${valueOf('database.retention_days')} / ${valueOf('database.slot_retention_days')} / ${valueOf('database.rollup_retention_days')}`)} days for all data, so every range up to 30 days works. Older data is deleted at the next hourly cleanup.</p>`;
}

// The extra rows of the Retention section, after its settings.
export function retentionTableHtml() {
  return `<div class="set-row set-note" data-extra="storage">${tableHtml()}</div>`;
}

// Redraws the table from the draft (called with every refresh of the rows).
export function refreshStorage(root) {
  const box = root.querySelector('.set-row[data-extra=storage]');
  if (!box) return;
  const html = tableHtml();
  if (box._html !== html) { box.innerHTML = html; box._html = html; }
}

// The lowered numbers of the draft that delete something now: [{kind, days, freed}].
export function loweredKinds() {
  return rows().filter((r) => r.kind.setting && r.days !== r.saved && freedNow(r.kind, r.days) > 0)
    .map((r) => ({ kind: r.kind, days: r.days, freed: freedNow(r.kind, r.days) }));
}

const LOWER_WORDS = { latency: 'latency history', router: 'router history', mesh: 'mesh history', line: 'cable line history', traffic: 'Internet use', speedtest: 'speed tests and router events', incident: 'incidents' };

// "Deletes about 380 MB of router history older than 90 days at the next hourly cleanup. This can't be undone."
export function lowerText(lowered) {
  const parts = lowered.map((l) => `about ${bytesText(l.freed)} of ${LOWER_WORDS[l.kind.id] || l.kind.label.toLowerCase()} older than ${l.days} days`);
  const list = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0];
  return `Deletes ${list} at the next hourly cleanup. This can't be undone.`;
}

// Reads the figures into U.storage (undefined while they are being read, null when there are none) and redraws the table.
export async function fetchStorage() {
  U.storage = S.server && S.server.can_edit ? undefined : null;
  if (U.storage === null) return;
  try {
    const res = await fetch('/api/storage', { cache: 'no-store' });
    U.storage = res.ok ? await res.json() : null;
  } catch (e) { U.storage = null; }
  refreshStorage(document.getElementById('settingsModal') || document);
}
