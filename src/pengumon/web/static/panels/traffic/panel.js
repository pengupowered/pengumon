// Internet use panel: the router's own traffic statistics (the `traffic` feature, a device offering traffic_usage): download
// and upload per hour, the devices and the apps that used the most. model.js polls GET /api/devices/{id}/traffic and makes the
// view model; view.js is the standard view (docs/themes.md, "Drawing a card"); client.js is the dialog of one device.

import { initTraffic } from './model.js';
import './client.js';
import './help.js';

export default {
  id: 'traffic',
  title: 'Internet use',
  tab: 'network',
  icon: 'chart',
  available: (ctx) => ctx.devicesOffering('traffic_usage').length > 0,
  reason: (ctx) => ctx.reason(['traffic'], 'Internet use', 'Add your router in Settings > Devices and services.'),
  mount: (section, ctx) => initTraffic(ctx.devicesOffering('traffic_usage')[0])
};
