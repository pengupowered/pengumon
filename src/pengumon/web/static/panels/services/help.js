// Help texts of this panel and its dialogs (help.js shows them in the popover). Numbers quoted here come from the
// code that applies them (services/checker.py) and change with it.

import { registerHelp } from '../../js/help.js';

registerHelp({
  'service.status': 'The result of a regular health check of an app on this host (an HTTP request to its health address). '
    + 'Up answers correctly. Degraded failed once or twice in a row. Down failed 3 checks in a row (the default; configurable) and is '
    + 'logged as an incident. Checking has not been tested yet. A push alert goes out only when it stays down for a while, '
    + 'so a restart during a deploy stays quiet.',
  'service.latency': 'How long the service took to answer its last health check, in milliseconds. A service that '
    + 'is up but slowly answering is often struggling; compare it with its usual figure rather than a fixed limit.'
});
