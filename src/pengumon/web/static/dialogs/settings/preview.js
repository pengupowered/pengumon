// The outage-rules preview: "with these rules the last 7 days would have had 4 incidents instead of 9". The server
// replays the stored samples through the classifier as a background job (POST /api/settings/preview, then polling
// GET /api/settings/preview/{id}); the page shows its progress and its result, and marks the result stale once the draft
// changes. Only classifier.*, line.down_mbps / line.up_mbps and alerts.* are rules the replay can judge.

import { escapeHtml } from '../../js/escape.js';
import { localFormat } from '../../js/clock.js';
import { canon } from './model.js';
import { detailText } from './problems.js';
import { S, valueOf } from './store.js';

const RULE_KEYS = /^(classifier\.|alerts\.|line\.(down|up)_mbps$)/;

const P = { id: null, job: null, error: '', sent: null, timer: 0, days: null, running: false, seq: 0, canceled: false };

// The draft's rule keys (a reset one is null).
function ruleValues() {
  return Object.fromEntries(Object.entries(S.draft.values).filter(([key]) => RULE_KEYS.test(key)));
}
export const hasRules = () => Object.keys(ruleValues()).length > 0;
export const shown = () => hasRules() || !!P.job || !!P.error || P.running || P.canceled;
const stale = () => !!P.job && P.sent !== canon(ruleValues());

const retention = () => Number(valueOf('database.retention_days')) || 7;
export const defaultDays = () => Math.min(7, retention());

const when = (ts) => localFormat(ts * 1000, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
const span = (seconds) => (seconds >= 3600 ? `${Math.round(seconds / 360) / 10} h` : seconds >= 60 ? `${Math.round(seconds / 60)} min` : `${Math.round(seconds)} s`);

// "1 incident instead of 3", or "1 incident, the same as now" when the draft rules change nothing.
const phrase = (noun, d, c) => `<b class="num">${Number(d)}</b> ${noun}${d === 1 ? '' : 's'} ${d === c ? '(unchanged)' : `instead of <b class="num">${Number(c)}</b>`}`;

// An alert channel is set up (ntfy, or a webhook with its URL): without one there is nobody to alert, so the counts are left out.
const hasChannel = () => Object.entries((S.server && S.server.secrets) || {}).some(([slot, st]) => st.set && /^(ntfy\.url|webhook\..*\.url)$/.test(slot));

const hasAlerts = (job) => hasChannel() && [job.draft.alerts, job.current.alerts].every((n) => n !== null && n !== undefined);

function counts(job) {
  const d = job.draft;
  const c = job.current;
  const line = [phrase('incident', d.incidents, c.incidents)];
  if (hasAlerts(job)) line.push(phrase('alert', d.alerts, c.alerts));
  return line.join('; ');
}

// The alert count is per outage (the drops of one outage send one alert), so it can be lower than the incident count.
const alertsNote = (job) => (hasAlerts(job) ? '<p class="fine" data-alerts-note>Alerts are counted per outage: the drops of one outage send one alert.</p>' : '');

// Said in the open, not in a tooltip: the stored count is not comparable with the two replayed ones.
function recordedNote(job) {
  if (job.recorded === null || job.recorded === undefined) return '';
  return `<p class="fine" data-recorded>Pengumon recorded ${Number(job.recorded)} incident${job.recorded === 1 ? '' : 's'} in that time. That number is not comparable with the two above: it was counted with the rules and the path of the time, the replay uses today's for both.</p>`;
}

function changeList(job) {
  const gone = job.changes.filter((c) => c.in === 'current');
  const added = job.changes.filter((c) => c.in === 'draft');
  const moved = job.changes.filter((c) => c.in === 'changed');
  const length = (c) => (c.end ? escapeHtml(span(c.end - c.start)) : 'ongoing');
  const item = (c) => `<li>${escapeHtml(when(c.start))}, ${escapeHtml(c.kind.replace(/_/g, ' '))}, ${escapeHtml(c.severity)}, ${length(c)}.</li>`;
  const lengthNow = (c) => (c.was_end ? escapeHtml(span(c.was_end - c.was_start)) : 'ongoing');
  // only what differs. An outage still going on opens a few seconds apart under two rule sets: say that, not the same minute twice.
  const change = (c) => {
    const diffs = [];
    const offset = c.start - c.was_start;
    if (when(c.was_start) !== when(c.start)) diffs.push(`starts ${escapeHtml(when(c.was_start))} now, ${escapeHtml(when(c.start))} with the draft`);
    else if (Math.abs(offset) >= 1) diffs.push(`opens ${escapeHtml(span(Math.abs(offset)))} ${offset < 0 ? 'earlier' : 'later'} with the draft`);
    if (c.was_severity !== c.severity) diffs.push(`${escapeHtml(c.was_severity)} now, ${escapeHtml(c.severity)} with the draft`);
    if (lengthNow(c) !== length(c)) diffs.push(`${lengthNow(c)} now, ${length(c)} with the draft`);
    const kind = escapeHtml(c.kind.replace(/_/g, ' '));
    return `<li>${kind}: ${diffs.length ? diffs.join('; ') : 'the same start and end'}${c.end ? '' : '; still going on'}.</li>`;
  };
  const block = (title, list, fn = item) => (list.length ? `<p class="fine top">${title}</p><ul class="set-changes">${list.map(fn).join('')}</ul>` : '');
  return block('Would no longer open:', gone) + block('Would newly open:', added) + block('Would open differently (same outage, other start, end or level):', moved, change);
}

export function previewHtml() {
  const job = P.job;
  const days = P.days ?? defaultDays();
  const head = `<div class="set-prev-h"><h3 class="section-t">Preview</h3>
    <label class="set-days">Preview the last <input class="input num" type="text" inputmode="numeric" id="settingsPreviewDays" value="${escapeHtml(days)}" size="3" aria-label="Days to replay"> days</label>
    <button type="button" class="btn sm outline" data-action="settingsPreviewRun" data-needs-edit${P.running ? ' disabled' : ''}>${P.running ? 'Running…' : 'Run preview'}</button>${P.running ? '<button type="button" class="btn sm" data-action="settingsPreviewCancel">Cancel</button>' : ''}</div>`;
  let body = '';
  if (P.error) body = `<div class="result st-crit" id="settingsPreviewResult" role="status"><span>${escapeHtml(P.error)}</span></div>`;
  else if (P.running || (job && job.state === 'running')) {
    const p = Math.round(((job && job.progress) || 0) * 100);
    body = `<div class="set-progress" id="settingsPreviewResult"><progress max="100" value="${Number(p)}" aria-label="Preview progress"></progress><span class="num">${Number(p)}%</span></div>`;
  } else if (P.canceled) {
    body = '<p class="fine top" id="settingsPreviewResult" role="status">Preview canceled. Nothing was changed.</p>';
  } else if (job && job.state === 'done' && job.draft && job.current) {
    body = `<div class="result st-${stale() ? 'unknown' : 'good'}${stale() ? ' stale' : ''}" id="settingsPreviewResult" role="status"><span>With these rules the last ${escapeHtml(days)} day${days === 1 ? '' : 's'} would have had ${counts(job)}.
      ${stale() ? '<b>Out of date: the draft changed since. Run the preview again.</b>' : ''}</span></div>
      ${recordedNote(job)}${alertsNote(job)}${(job.notes || []).map((n) => `<p class="fine">${escapeHtml(n)}</p>`).join('')}${changeList(job)}`;
  } else if (job && (job.state === 'failed' || job.state === 'canceled')) {
    body = `<div class="result st-crit" id="settingsPreviewResult" role="status"><span>The preview ${job.state === 'failed' ? `failed${job.error ? `: ${escapeHtml(job.error)}` : ''}` : 'was canceled'}.</span></div>`;
  } else {
    body = '<p class="fine top">Replays the stored samples through the classifier with the draft rules. Nothing is changed and nothing is sent.</p>';
  }
  return head + body;
}

let render = () => {};
export const afterPreviewChange = (fn) => { render = fn; };

async function poll(seq) {
  while (P.running && seq === P.seq) {
    await new Promise((r) => { P.timer = setTimeout(r, 700); });
    if (seq !== P.seq) return;
    try {
      const res = await fetch(`/api/settings/preview/${P.id}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(String(res.status));
      const job = await res.json();
      if (seq !== P.seq) return; // stopped or run again while the answer came
      P.job = job;
    } catch (e) {
      if (seq !== P.seq) return;
      P.error = 'The preview could not be read.';
      P.running = false;
      break;
    }
    if (P.job.state !== 'running') P.running = false;
    render();
  }
  render();
}

export async function runPreview(daysText) {
  if (P.running) return;
  const days = Number(daysText);
  const max = retention();
  P.error = '';
  P.canceled = false;
  if (!Number.isInteger(days) || days < 1 || days > max) {
    P.days = daysText;
    P.error = `Enter a whole number of days from 1 to ${max}.`;
    render();
    return;
  }
  P.days = days;
  const values = ruleValues();
  P.sent = canon(values);
  P.running = true;
  P.job = null;
  const seq = ++P.seq;
  render();
  try {
    const res = await fetch('/api/settings/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values, days }) });
    if (seq !== P.seq) { // canceled while the job was being made: this run's answer is not wanted, and its job is dropped
      if (res.ok) res.json().then((j) => fetch(`/api/settings/preview/${j.id}`, { method: 'DELETE' })).catch(() => {});
      return;
    }
    if (res.status === 429) throw new Error('Another preview is running. Try again in half a minute.');
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(detailText(body.detail, `The preview was refused (${res.status}).`));
    }
    const made = await res.json();
    if (seq !== P.seq) { fetch(`/api/settings/preview/${made.id}`, { method: 'DELETE' }).catch(() => {}); return; }
    P.id = made.id;
  } catch (e) {
    if (seq !== P.seq) return;
    P.error = e.message || 'The preview could not be started.';
    P.running = false;
    render();
    return;
  }
  poll(seq);
}

// The dialog closed or the draft was thrown away: stop polling and let the server drop the job.
export function stopPreview() {
  P.seq++;
  clearTimeout(P.timer);
  const id = P.id;
  P.id = null;
  P.job = null;
  P.error = '';
  P.running = false;
  P.sent = null;
  P.days = null;
  P.canceled = false;
  if (id) fetch(`/api/settings/preview/${id}`, { method: 'DELETE' }).catch(() => {});
}

// The Cancel button: stop the replay (the server drops the job) and keep the panel, with the days typed.
export function cancelPreview() {
  const days = P.days;
  stopPreview();
  P.days = days;
  P.canceled = true;
  render();
}
