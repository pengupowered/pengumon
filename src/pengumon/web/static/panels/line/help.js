// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here come from the
// code that applies them (lines/docsis.py) and change with it.

import { registerHelp } from '../../js/help.js';

registerHelp({
  'docsis.dspower': 'Downstream power: how strongly the modem receives the cable signal, averaged over the downstream '
    + 'channels, in dBmV. Close to 0 is ideal; within +-8 dBmV is good (the green zone of the gauge), '
    + 'beyond that the reading is graded warning. Too strong or too weak causes dropouts. The whisker on '
    + 'the gauge is the spread across the channels.',
  'docsis.snr': 'Downstream signal-to-noise ratio in dB: how far the signal stands above the noise, averaged over the '
    + 'channels that report it. 33 dB and above is good. Below 33 dB it is graded warning and below 30 dB '
    + 'critical: noise that high corrupts packets. Noise usually comes from loose or damaged cable, '
    + 'splitters or interference.',
  'docsis.uspower': 'Upstream transmit power: how hard the modem has to shout to be heard, averaged over the upstream '
    + 'channels, in dBmV. 38 to 49 dBmV is good. Above 49 dBmV it is graded warning and above 51 dBmV '
    + 'critical (below 38 and 35 likewise): the modem is near its limit, which points to a line problem '
    + 'between your home and the street.',
  'docsis.uncorr': 'Uncorrectable codewords: data blocks the modem could not repair and had to drop, new since the '
    + 'previous reading. pengumon judges how serious they are, not whether there are any: by their share of '
    + 'all codewords when the modem reports that (warning from 1e-6, critical from 1e-4), else by the rate '
    + '(warning from 60 a minute, critical above 500 a minute) or by errors repeating on consecutive '
    + 'readings. A single short burst is normal and graded good. The card says on how many channels and '
    + 'frequencies they fell, over what interval, and whether the probes lost packets meanwhile.',
  'docsis.cumulative': 'The modem\'s own counter of codewords it could not repair, counting from its last power-on or '
    + 'reboot. A big number does not mean a problem now; watch the "New errors" column.',
  'channel.heatmap': 'One row per channel (DS downstream, US upstream), one column per reading, coloured by health with '
    + 'pengumon\'s limits for the line (the gauges\' zones): green good, amber warning, red critical, grey '
    + 'no reading. SNR: 33 dB and above is good, below 30 dB critical. Power: downstream within +-8 dBmV, '
    + 'upstream 38 to 49 dBmV. Click a row for that channel\'s detail. A channel that stays red while the '
    + 'others are green points at one frequency (often interference); all rows going red together point at '
    + 'the cable.'
});
