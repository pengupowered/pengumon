// Line: one card per enabled device offering access_line. The card's common part (name, status, errors, setup) knows
// no technology; the body and the detail view come from the adapter for the device's line_technology (lines/*.js).
// A technology without an adapter gets the common part and a sentence saying so.

import { cloneTemplate, escapeHtml, setText } from '../../js/util.js';
import { watchDevice } from '../../js/device-feed.js';
import { paintCommon, placeCard } from '../../js/device-panel.js';
import docsis from './docsis.js';
import './help.js';

const ADAPTERS = { docsis };
const TECH_NAMES = { docsis: 'DOCSIS', fiber: 'Fiber', dsl: 'DSL' };

function mountLine(section, ctx, device) {
  const { root, refs } = cloneTemplate('tpl-line');
  const adapter = ADAPTERS[device.line_technology] || null;
  // the configured model, else a generic name from the technology's adapter ("Line" without one): never the host twice
  setText(refs.title, device.model ? String(device.model).replace(/_/g, ' ') : ((adapter && adapter.defaultTitle) || 'Line'));
  const tech = device.line_technology ? (TECH_NAMES[device.line_technology] || device.line_technology) : 'line';
  setText(refs.meta, [tech, device.host].filter(Boolean).join(' · '));
  let render = null;
  if (adapter) {
    render = adapter.mount(refs.body, device);
    const a = adapter.detailAction;
    refs.actions.innerHTML = `<button type="button" class="btn sm outline" data-action="${a.action}" data-arg="${escapeHtml(device.id)}"><svg class="i" aria-hidden="true"><use href="#i-${a.icon}"/></svg>${a.label}</button>`;
  } else {
    refs.body.innerHTML = `<p class="cap-note">pengumon has no detail view for ${escapeHtml(tech)} lines yet: the card shows the device's status only.</p>`;
  }
  placeCard(section, root, refs, device);

  // The live summary is short; when the adapter needs the full reading, it is fetched once per new reading.
  let drawnAt = null;
  let detail = null;
  const draw = (snap) => {
    if (!render || snap.timestamp === drawnAt) return;
    drawnAt = snap.timestamp;
    render(snap);
  };
  watchDevice(ctx.bus, device, (snap) => {
    if (!paintCommon(root, refs, device, snap, 'Line up') || !render) return;
    if (!adapter.needsDetail(snap)) { detail = snap; draw(snap); return; }
    if (detail && detail.timestamp === snap.timestamp) { draw(detail); return; }
    fetch(`/api/devices/${encodeURIComponent(device.id)}`).then((r) => (r.ok ? r.json() : null)).then((full) => {
      if (full && full.timestamp === snap.timestamp) { detail = full; draw(full); } else draw(snap);
    }).catch(() => draw(snap));
  });
}

export default {
  id: 'line',
  title: 'Line',
  tab: 'line',
  icon: 'modem',
  instances: (ctx) => ctx.devicesOffering('access_line').map((d) => ({ key: d.id, device: d })),
  available: (ctx) => ctx.devicesOffering('access_line').length > 0,
  reason: (ctx) => ctx.reason(['line_health'], 'Line'),
  mount: (section, ctx, inst) => mountLine(section, ctx, inst.device)
};
