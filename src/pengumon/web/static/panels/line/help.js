// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here are read from the
// settings (js/settings-values.js: line_signal.*), so they say what the page uses now.

import { registerHelp } from '../../js/help.js';
import { val } from '../../js/settings-values.js';

const LS = 'line_signal.';
const n = (v, key, fallback) => val(v, LS + key, fallback);
const ratio = (v, key, fallback) => Number(n(v, key, fallback)).toExponential();

registerHelp({
  'docsis.dspower': (v) => 'Downstream power: how strongly the modem receives the cable signal, averaged over the downstream '
    + 'channels, in dBmV. Close to 0 is ideal; within +-' + n(v, 'ds_power_warning_abs_dbmv', 8) + ' dBmV is good (the green zone of the gauge), '
    + 'beyond that the reading is graded warning. Too strong or too weak causes dropouts. The whisker on '
    + 'the gauge is the spread across the channels.',
  'docsis.snr': (v) => 'Downstream signal-to-noise ratio in dB: how far the signal stands above the noise, averaged over the '
    + 'channels that report it. ' + n(v, 'ds_snr_warning_db', 33) + ' dB and above is good. Below ' + n(v, 'ds_snr_warning_db', 33) + ' dB it is graded warning and below '
    + n(v, 'ds_snr_critical_db', 30) + ' dB critical: noise that high corrupts packets. Noise usually comes from loose or damaged cable, '
    + 'splitters or interference.',
  'docsis.uspower': (v) => 'Upstream transmit power: how hard the modem has to shout to be heard, averaged over the upstream '
    + 'channels, in dBmV. ' + n(v, 'us_power_low_warning_dbmv', 38) + ' to ' + n(v, 'us_power_warning_dbmv', 49) + ' dBmV is good. Above '
    + n(v, 'us_power_warning_dbmv', 49) + ' dBmV it is graded warning and above ' + n(v, 'us_power_critical_dbmv', 51) + ' dBmV '
    + 'critical (below ' + n(v, 'us_power_low_warning_dbmv', 38) + ' and ' + n(v, 'us_power_low_critical_dbmv', 35) + ' likewise): the modem is near its limit, which points to a line problem '
    + 'between your home and the street.',
  'docsis.uncorr': (v) => 'Uncorrectable codewords: data blocks the modem could not repair and had to drop, new since the '
    + 'previous reading. pengumon judges how serious they are, not whether there are any: by their share of '
    + 'all codewords when the modem reports that (warning from ' + ratio(v, 'uncorrectable_ratio_warning', 1e-6) + ', critical from '
    + ratio(v, 'uncorrectable_ratio_critical', 1e-4) + '), else by the rate (warning from ' + n(v, 'uncorrectable_warning_per_minute', 60)
    + ' a minute, critical above ' + n(v, 'uncorrectable_critical_per_minute', 500) + ' a minute) or by errors repeating on consecutive '
    + 'readings. A single short burst is normal and graded good. The card says on how many channels and '
    + 'frequencies they fell, over what interval, and whether the probes lost packets meanwhile.',
  'docsis.cumulative': 'The modem\'s own counter of codewords it could not repair, counting from its last power-on or '
    + 'reboot. A big number does not mean a problem now; watch the "New errors" column.',
  'channel.heatmap': (v) => 'One row per channel (DS downstream, US upstream), one column per reading, colored by health with '
    + 'Pengumon\'s limits for the line (the gauges\' zones): green good, amber warning, red critical, gray '
    + 'no reading. SNR: ' + n(v, 'ds_snr_warning_db', 33) + ' dB and above is good, below ' + n(v, 'ds_snr_critical_db', 30)
    + ' dB critical. Power: downstream within +-' + n(v, 'ds_power_warning_abs_dbmv', 8) + ' dBmV, upstream ' + n(v, 'us_power_low_warning_dbmv', 38)
    + ' to ' + n(v, 'us_power_warning_dbmv', 49) + ' dBmV. Click a row for that channel\'s detail. A channel that stays red while the '
    + 'others are green points at one frequency (often interference); all rows going red together point at '
    + 'the cable.'
});
