// Host services panel: the apps on this host whose health is checked (config: service_checks).

import { featureAvailable, missingNote } from '../../js/devices.js';
import { initServices } from './services.js';
import './help.js';

export default {
  id: 'services',
  title: 'Host services',
  tab: 'network',
  icon: 'service',
  available: () => featureAvailable('host_services'),
  reason: () => missingNote(['host_services'], 'Host services'),
  mount: () => initServices()
};
