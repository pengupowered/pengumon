// Line: one card per enabled device offering access_line. The card's common part (name, status, errors, setup) knows
// no technology; the readings and the detail view come from the adapter for the device's line_technology (docsis.js).
// A technology without an adapter gets the common part and a sentence saying so.
//
// This is the card's model: it watches the device (fetching the full reading when the adapter needs it), keeps the
// frame (title, badge, read time, the detail button) and makes the view model every view of the card draws (view.js is
// the standard one; docs/themes.md, "Drawing a card"): { error, dim, note, readings }, where `readings` is the
// adapter's {gauges: [...], errors, channels} (null before the first reading) and `note` the sentence for a technology
// with no adapter.

import { cloneTemplate, escapeHtml, setText } from '../../js/util.js';
import { createCard } from '../../js/cards.js';
import { watchDevice } from '../../js/device-feed.js';
import { errorOf, paintCommon, partPusher, placeCard } from '../../js/device-panel.js';
import docsis from './docsis.js';
import standard from './view.js';

const ADAPTERS = { docsis };
const TECH_NAMES = { docsis: 'DOCSIS', fiber: 'Fiber', dsl: 'DSL' };

const noteFor = (tech) => `Pengumon has no detail view for ${tech} lines yet: the card shows the device's status only.`;

// The view model for the parts a reading makes; `error` and `dim` come from the snapshot's status (device-panel.js).
export function present({ note, readings }) {
  return { error: null, dim: false, note: note || null, readings: readings || null };
}

// Made-up lines for the parity test of the card's views (tests/ui/steps/core.mjs): a line with every part (the adapter's
// `sample`), and
// (fixture.variants) the least a line driver can offer, a technology with no adapter (the common part and a sentence).
export function fixture() {
  const [adapter] = Object.values(ADAPTERS); // the first adapter's made-up device and reading
  return present({ readings: adapter.reader(adapter.sample.device)(adapter.sample.snap) });
}

fixture.variants = [() => present({ note: noteFor('Fiber') })];

export function mountLine(section, ctx, device) {
  const { root, refs } = cloneTemplate('tpl-line');
  const adapter = ADAPTERS[device.line_technology] || null;
  // the configured model, else a generic name from the technology's adapter ("Line" without one): never the host twice
  setText(refs.title, device.model ? String(device.model).replace(/_/g, ' ') : ((adapter && adapter.defaultTitle) || 'Line'));
  const tech = device.line_technology ? (TECH_NAMES[device.line_technology] || device.line_technology) : 'line';
  setText(refs.meta, [tech, device.host].filter(Boolean).join(' · '));
  if (adapter) {
    const a = adapter.detailAction;
    refs.actions.innerHTML = `<button type="button" class="btn sm outline" data-action="${a.action}" data-arg="${escapeHtml(device.id)}"><svg class="i" aria-hidden="true"><use href="#i-${a.icon}"/></svg>${a.label}</button>`;
  }
  placeCard(section, root, refs, device);
  const card = createCard({ panel: 'line', root: root.querySelector('.panel-b'), standard, fixture });
  const read = adapter ? adapter.reader(device) : null;

  let snap = null;
  let readings = null;
  const pushed = partPusher(card);
  const push = () => pushed({ ...present({ note: adapter ? null : noteFor(tech), readings }), error: errorOf(snap, device), dim: !!snap && snap.status === 'error' });
  push();

  // The live summary is short; when the adapter needs the full reading, it is fetched once per new reading.
  let drawnAt = null;
  let detail = null;
  const draw = (s) => {
    if (s.timestamp === drawnAt) return;
    drawnAt = s.timestamp;
    readings = read(s);
    push();
  };
  watchDevice(ctx.bus, device, (s) => {
    snap = s;
    const reading = paintCommon(root, refs, device, s, 'Line up');
    push();
    if (!reading || !read) return;
    if (!adapter.needsDetail(s)) { detail = s; draw(s); return; }
    if (detail && detail.timestamp === s.timestamp) { draw(detail); return; }
    fetch(`/api/devices/${encodeURIComponent(device.id)}`).then((r) => (r.ok ? r.json() : null)).then((full) => {
      if (full && full.timestamp === s.timestamp) { detail = full; draw(full); } else draw(s);
    }).catch(() => draw(s));
  });
}
