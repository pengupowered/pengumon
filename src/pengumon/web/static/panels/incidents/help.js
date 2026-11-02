// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here come from the
// code that applies them (engine/load.py, config.py busy_load_pct) and change with it.

import { registerHelp } from '../../js/help.js';

registerHelp({
  'incident.cause': 'Where the fault sat, as the classifier placed it; the timeline has one lane per cause. Router: your own '
    + 'router or mesh (Path health\'s Router). Line: the gateway or modem and the line to your provider (Path health\'s Line). '
    + 'ISP: the provider\'s hops and beyond (ISP hops, backbone). Internet: only the public servers failed. DNS: '
    + 'name lookups failed while pings worked. Load: delay explained by a busy line. The % load chip is the peak WAN load '
    + 'around the event, as a share of the line capacity.',
  'line.load': 'How busy the internet line is, as a percentage of its capacity, from the router\'s WAN byte counters '
    + '(the larger of download and upload). Above 60% (busy_load_pct) delay and loss at ISP hops count as expected, '
    + 'because a line near its limit queues packets; those incidents are marked as busy and can be hidden. Without '
    + 'fresh router data the load is unknown and the verdict ignores it.'
});
