// Host services panel: the apps on this host whose health is checked (config: service_checks). model.js keeps the
// services and makes the view model; view.js is the standard view (docs/themes.md, "Drawing a card").

import { featureAvailable } from '../../js/devices.js';
import { initServices } from './model.js';
import './help.js';

export default {
  id: 'services',
  title: 'Host services',
  tab: 'network',
  icon: 'service',
  available: () => featureAvailable('host_services'),
  reason: () => 'Host services: nothing to watch yet. Add a service in Settings > Devices and services.',
  mount: () => initServices()
};
