// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here come from the
// code that applies them (engine/classifier) and change with it.

import { registerHelp } from '../../js/help.js';

registerHelp({
  'diagnosis': 'The classifier\'s one-line verdict, recomputed continuously from the last 10 probes of every target. It '
    + 'weighs which categories are failing: for example, if your router answers but the hops beyond it do not, the '
    + 'problem is in the line or the ISP. Healthy means every hop is within '
    + 'its loss and latency limits. A fault that lasts long enough is saved to the incident log below.'
});
