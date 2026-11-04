// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here come from the
// code that applies them (engine/classifier) and change with it.

import { registerHelp } from '../../js/help.js';

registerHelp({
  'diagnosis': 'The classifier\'s one-line verdict, recomputed continuously from the last 10 probes of every target. It '
    + 'weighs which categories are failing: for example, if your router and gateway answer but the provider\'s first '
    + 'router does not, the problem is your line or that router; if the gateway itself stops answering, it is the '
    + 'gateway, not the line. Healthy means every hop is within '
    + 'its loss and latency limits. A fault that lasts long enough is saved to the incident log below.'
});
