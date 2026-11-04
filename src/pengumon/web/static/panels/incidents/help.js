// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here are read from the
// settings (js/settings-values.js: classifier.busy_load_pct), so they say what the page uses now.

import { registerHelp } from '../../js/help.js';
import { val } from '../../js/settings-values.js';

registerHelp({
  'incident.cause': 'Where the fault sat, as the classifier placed it; the timeline has one lane per cause. Router: your own '
    + 'router or mesh (Path health\'s Router). Gateway: your gateway or modem stopped answering on its home-side address '
    + '(Path health\'s Gateway); those pings do not use the line, so it is the gateway itself or the link to it. Line: loss '
    + 'starts at the provider\'s first router while the gateway answers, so your line or that router. ISP: the provider\'s '
    + 'network beyond it (backbone, upstream latency). Internet: only the public servers failed. DNS: '
    + 'name lookups failed while pings worked. Load: delay explained by a busy line. The % load chip is the peak WAN load '
    + 'around the event, as a share of the line capacity.',
  'line.load': (v) => 'How busy the internet line is, as a percentage of its capacity, from the router\'s WAN byte counters '
    + '(the larger of download and upload). Above ' + val(v, 'classifier.busy_load_pct', 60) + '% (the busy level in Settings) delay and loss at ISP hops count as expected, '
    + 'because a line near its limit queues packets; those incidents are marked as busy and can be hidden. Without '
    + 'fresh router data the load is unknown and the verdict ignores it.'
});
