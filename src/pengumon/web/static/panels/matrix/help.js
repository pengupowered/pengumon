// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here are read from the
// settings (js/settings-values.js: prober.hop_status.*), so they say what the page uses now, not what it used to.

import { registerHelp } from '../../js/help.js';
import { val } from '../../js/settings-values.js';

const HOP = 'prober.hop_status.';
const pct = (v, key, fallback) => `${Math.round(val(v, HOP + key, fallback) * 100)}%`;
const ms = (v, key, fallback) => `${val(v, HOP + key, fallback)} ms`;

registerHelp({
  'hop.category': 'The bold name is the target\'s place in the path, in the words Path health uses: Router is a device on '
    + 'your own network (category local), Gateway is your modem or the box your provider gave you, probed on its home-side address (gateway), ISP hop N is a '
    + 'router inside your provider\'s network (isp), Internet with an address is a well-known server such as 1.1.1.1 '
    + '(public), and DNS lookup is a name lookup test instead of a ping (dns). A target you added yourself (custom) keeps '
    + 'its name. Under it: the name from your config and the address. A fault is placed at the nearest stage that stops '
    + 'answering, which is how the banner can tell your router from your provider.',
  'hop.loss': (v) => 'The share of the last 60 probes that got no answer (about the last minute: probes go out once a second). The '
    + 'Lost since start column counts every probe since pengumon started. Normal is 0%. A hop shows amber above ' + pct(v, 'degraded_loss_fraction', 0.1) + ' and red above ' + pct(v, 'down_loss_fraction', 0.4)
    + ' of its last 10 probes lost. Some ISP routers drop probes aimed at themselves while still forwarding traffic, so loss on '
    + 'a middle hop alone is not a fault if the hops after it are clean.',
  'hop.latency': (v) => 'Round-trip time in milliseconds: how long a probe takes to reach the target and come back. Min, Avg and '
    + 'Max cover the last 60 probes and Last is the newest one; LOST means it got no answer. The limits before '
    + 'a hop turns amber are ' + ms(v, 'local_latency_ms', 20) + ' for local, ' + ms(v, 'gateway_latency_ms', 30) + ' for the gateway, '
    + ms(v, 'public_latency_ms', 60) + ' for public servers and ' + ms(v, 'dns_latency_ms', 100) + ' for DNS (a hop can have its own '
    + 'limit: path editor, More). ISP and custom hops are judged on loss only. A busy line adds delay without being faulty.',
  'hop.jitter': (v) => 'How much the round-trip time varies from probe to probe, in milliseconds. A steady line has low jitter '
    + 'even if it is slow; high jitter breaks calls and games. The limits before a hop turns amber are ' + ms(v, 'local_jitter_ms', 10)
    + ' for local, ' + ms(v, 'gateway_jitter_ms', 15) + ' for the gateway, ' + ms(v, 'public_jitter_ms', 25) + ' for public servers and '
    + ms(v, 'dns_jitter_ms', 40) + ' for DNS.'
});
