// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here are read from the
// settings (js/settings-values.js: router_levels.*, classifier.busy_load_pct, database.retention_days), so they say what
// the page uses now.

import { registerHelp } from '../../js/help.js';
import { val } from '../../js/settings-values.js';

registerHelp({
  'line.capacity': 'The line speed that load is measured against. It is the figure you set in config (line.down_mbps and '
    + 'line.up_mbps, "configured"), or when unset it is learned: the 99.9th percentile of the router\'s WAN rates over '
    + 'the last 7 days, with a floor of 10 Mbps, and only once 6 hours of samples exist ("learned"). Thin dashed lines on '
    + 'the router history WAN chart show it. A learned value is too low on a line that never gets fully used.',
  'router.cpu': (v) => 'How busy the router\'s processor was, in percent, averaged over its cores. The router only gives '
    + 'counters that count up since its boot, so Pengumon works the percentage out from the difference '
    + 'between two readings. The first reading after a start has none. Above ' + val(v, 'router_levels.cpu_warning_pct', 90) + '% it is graded warning: a '
    + 'router that busy adds delay for the whole network.',
  'router.mem': (v) => 'How much of the router\'s memory is in use. Above ' + val(v, 'router_levels.mem_warning_pct', 85) + '% it is graded warning and above '
    + val(v, 'router_levels.mem_critical_pct', 95) + '% critical, '
    + 'where crashes become likely. A figure that creeps up over days suggests a leak; a reboot clears it.',
  'router.wan': (v) => 'Current traffic through the router\'s internet port, in Mbit/s, for download and upload, worked out '
    + 'from two readings of the router\'s byte counters, so it is an average over the polling interval '
    + 'rather than an instant. The bar is the share of the line capacity: from the busy load '
    + '(' + val(v, 'classifier.busy_load_pct', 60) + '% in Settings) it is graded warning, from '
    + val(v, 'router_levels.wan_critical_pct', 95) + '% critical. Near capacity, expect '
    + 'higher ping times.',
  'router.clients': 'The number of devices the router currently counts as connected, wired and Wi-Fi. Where the router '
    + 'can list them, this opens the Clients list.',
  'router.history': (v) => 'CPU and memory, the CPU temperature (when the router reports it), WAN rates and the number of clients over 1 hour to ' + val(v, 'database.retention_days', 7) + ' days (how long readings are kept), from the router readings '
    + 'saved every 10 s. Longer ranges are averaged into time slices (solid lines are the average, dashed '
    + 'lines the peak). Use it to see whether a slowdown lined up with a busy line or a loaded router.',
  'clients.band': 'How the device is connected: wired, or Wi-Fi on the 2.4, 5 or 6 GHz band. 2.4 GHz reaches farther but '
    + 'is slower and more crowded; 5 and 6 GHz are faster at a shorter range.',
  'clients.signal': 'Signal strength (RSSI) of the Wi-Fi link in dBm, as the router or mesh node hears the device. Closer to '
    + '0 is stronger: above -60 dBm is excellent, above -67 good, above -75 fair, and -75 or below is weak and likely to be slow or drop.',
  'clients.speed': 'The Wi-Fi link rate the radio negotiated with the router (Tx / Rx), not traffic. It is the speed '
    + 'the connection could carry, not what the device is using. A phone on a fast link may be idle. How much a '
    + 'device really used is on the Internet use card. A low rate usually goes with a weak signal.',
  'clients.connected': 'How long the device has been on the network in its current session. It restarts when the device '
    + 'disconnects or the router reboots.',
  'clients.node': 'The mesh node (router or satellite) the device is connected to. Signal and link speed '
    + 'are measured against that node, not against the main router.'
});
