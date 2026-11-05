// Preview: the draft's hops not probed live are probed once a second for 30 s, nothing recorded (POST /api/path/preview).

import { readPreview, startPreview, stopPreview } from '../../js/path-api.js';
import { canEdit, escapeHtml } from '../../js/util.js';
import { $, note, payload, pe, probedLive } from './draft.js';
import { paintLive, PREVIEW_S } from './render.js';

// ---------- preview ----------
export function stopRunningPreview() {
  if (pe.preview && pe.preview.state === 'running') stopPreview(pe.preview.id);
  if (pe.preview) pe.preview.state = 'stopped';
  pe.preview = null;
  const b = $('btnPreviewPath');
  if (b) b.querySelector('span').textContent = 'Preview';
}

export async function runPreview() {
  if (!pe.draft || !canEdit()) return;
  if (pe.preview && pe.preview.state === 'running') { stopRunningPreview(); paintLive(); return; } // a second press stops it
  const unprobed = pe.draft.hops.filter((h) => !probedLive(h) && h.enabled !== false);
  if (!unprobed.length) { note('unknown', 'Every hop of the draft is probed already: its live figures are on its card.'); return; }
  const label = $('btnPreviewPath').querySelector('span');
  try {
    if (pe.preview) stopRunningPreview(); // a finished one: its figures give way to the new run's
    const { id, probed } = await startPreview(payload());
    pe.preview = { id, state: 'running', elapsed: 0, hops: {}, addresses: Object.fromEntries(pe.draft.hops.map((h) => [h.id, h.address])) };
    label.textContent = 'Stop preview';
    const n = (probed || unprobed).length;
    note('unknown', `<b>Previewing ${n} hop${n === 1 ? '' : 's'}</b> for ${PREVIEW_S} s: one probe a second, nothing recorded. Their figures fill in on the cards.`);
    paintLive();
    const mine = pe.preview;
    while (mine.state === 'running') {
      await new Promise((r) => setTimeout(r, 1000));
      if (mine.state !== 'running') break;
      const r = await readPreview(id);
      Object.assign(mine, { hops: r.hops || {}, elapsed: r.elapsed_s || 0 });
      if (r.state === 'done') mine.state = 'done';
      paintLive();
    }
    if (mine.state === 'done') {
      label.textContent = 'Preview';
      note('good', `<b>Preview done.</b> ${PREVIEW_S} s of probes are on the cards of the hops that are not probed yet. Apply to keep the path.`);
    }
  } catch (e) {
    if (pe.preview) pe.preview.state = 'failed';
    label.textContent = 'Preview';
    note(e.unavailable ? 'warn' : 'crit', `<b>No preview.</b> ${escapeHtml(e.message)}`);
  }
}
