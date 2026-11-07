// Router: one card per enabled device offering wan_counters or router_system. model.js watches the device and makes the
// view model; view.js is the standard view (docs/themes.md, "Drawing a card").

import { initTopology } from './topology.js';
import { mountRouter } from './model.js';
import './clients.js';
import './history.js';
import './help.js';

export default {
  id: 'router',
  title: 'Router',
  tab: 'network',
  icon: 'router',
  instances: (ctx) => ctx.devicesOffering('wan_counters', 'router_system').map((d) => ({ key: d.id, device: d })),
  available: (ctx) => ctx.devicesOffering('wan_counters', 'router_system').length > 0,
  reason: (ctx) => ctx.reason(['router_system', 'line_load'], 'Router telemetry', 'Add your router in Settings > Devices and services.'),
  prepare: (ctx) => initTopology(ctx),
  mount: (section, ctx, inst) => mountRouter(section, ctx, inst.device)
};
