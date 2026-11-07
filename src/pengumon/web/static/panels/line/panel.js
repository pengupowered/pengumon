// Line: one card per enabled device offering access_line. model.js watches the device and makes the view model; view.js
// is the standard view; the line technology's adapter is docsis.js (docs/themes.md, "Drawing a card").

import { mountLine } from './model.js';
import './help.js';

export default {
  id: 'line',
  title: 'Line',
  tab: 'line',
  icon: 'modem',
  instances: (ctx) => ctx.devicesOffering('access_line').map((d) => ({ key: d.id, device: d })),
  available: (ctx) => ctx.devicesOffering('access_line').length > 0,
  reason: (ctx) => ctx.reason(['line_health'], 'Line', 'Add your modem in Settings > Devices and services.'),
  mount: (section, ctx, inst) => mountLine(section, ctx, inst.device)
};
