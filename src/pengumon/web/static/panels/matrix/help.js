// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here come from the
// code that applies them (probe thresholds: config.py HopStatusConfig, ClassifierConfig; engine/classifier/metrics.py) and change with it.

import { registerHelp } from '../../js/help.js';

registerHelp({
  'hop.category': 'The bold name is the target\'s place in the path, in the words Path health uses: Router is a device on '
    + 'your own network (category local), Line is your modem or the box your provider gave you (gateway), ISP hop N is a '
    + 'router inside your provider\'s network (isp), Internet with an address is a well-known server such as 1.1.1.1 '
    + '(public), and DNS lookup is a name lookup test instead of a ping (dns). A target you added yourself (custom) keeps '
    + 'its name. Under it: the name from your config and the address. A fault is placed at the nearest stage that stops '
    + 'answering, which is how the banner can tell your router from your provider.',
  'hop.loss': 'The share of the last 60 probes that got no answer (about the last minute: probes go out once a second). The '
    + 'Lost since start column counts every probe since pengumon started. Normal is 0%. A hop shows amber above 10% and red above 40% of its '
    + 'last 10 probes lost. Some ISP routers drop probes aimed at themselves while still forwarding traffic, so loss on '
    + 'a middle hop alone is not a fault if the hops after it are clean.',
  'hop.latency': 'Round-trip time in milliseconds: how long a probe takes to reach the target and come back. Min, Avg and '
    + 'Max cover the last 60 probes and Last is the newest one; LOST means it got no answer. The default limits before '
    + 'a hop turns amber are 20 ms for local, 30 ms for the gateway, 60 ms for public servers and 100 ms for DNS. '
    + 'ISP and custom hops are judged on loss only. A busy line adds delay without being faulty.',
  'hop.jitter': 'How much the round-trip time varies from probe to probe, in milliseconds. A steady line has low jitter '
    + 'even if it is slow; high jitter breaks calls and games. The default limits before a hop turns amber are 10 ms '
    + 'for local, 15 ms for the gateway, 25 ms for public servers and 40 ms for DNS.'
});
