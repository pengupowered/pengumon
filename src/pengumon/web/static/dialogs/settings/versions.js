// Earlier versions: every Save is kept (the last 50). "Load" puts one in the page as a draft; Save then restores it.

import { escapeHtml } from '../../js/escape.js';
import { localFormat } from '../../js/clock.js';

const SOURCES = { import: 'first import', save: 'saved', restore: 'restored', yaml_import: 'imported file', upgrade: 'upgrade' };
const when = (ts) => localFormat(ts * 1000, { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });

export async function fetchVersions() {
  const res = await fetch('/api/settings/versions', { cache: 'no-store' });
  if (!res.ok) throw new Error(`The versions could not be read (${res.status}).`);
  return res.json();
}

export async function fetchVersion(n) {
  const res = await fetch(`/api/settings/versions/${n}`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Version ${n} could not be read (${res.status}).`);
  return res.json();
}

export function versionsHtml(list, current) {
  const rows = list.map((v) => `<li><span class="set-ver-t"><b>Revision ${Number(v.revision)}</b>${v.revision === current ? ' <span class="chip">current</span>' : ''}
      <span class="muted">${escapeHtml(when(v.applied_at))} · ${escapeHtml(SOURCES[v.source] || v.source)}</span><span class="set-ver-s">${escapeHtml(v.summary || '')}</span></span>
      <button type="button" class="btn sm" data-action="settingsLoadVersion" data-arg="${Number(v.revision)}" data-needs-edit>Load</button></li>`).join('');
  return `<h3 class="section-t">Earlier versions</h3>
    <p class="fine top">Every Save is kept (the last 50). Loading one puts it here as a draft; Save restores it.</p>
    <ul class="set-vers">${rows || '<li class="fine">No versions yet.</li>'}</ul>`;
}

export const savedAgo = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return when(ts);
};
