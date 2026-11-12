// Help texts of this panel and its dialog (help.js shows them in the popover).

import { registerHelp } from '../../js/help.js';

registerHelp({
  'traffic.use': 'How much the household used the internet, as your router counted it in its own Traffic Analyzer: download '
    + '(what came in) and upload (what went out) for each clock hour (for each day over 30 days: the 30 whole days up to the '
    + 'last midnight), which devices used the most, and which apps. The router '
    + 'publishes an hour a few minutes after it ends, and Pengumon reads it about once an hour, so the newest hour is '
    + '"not published yet" for a while: that is not zero use, only no figure. An hour Pengumon was not running for is '
    + '"not stored". Use it to see what filled the line during a slowdown: the incident inspector names the devices of the '
    + 'hours it covers. These are bytes at the router\'s internet port, in decimal units (1 GB = 1,000 MB).',
  'traffic.apps': 'The router sorts what it sees into apps (by their servers and ports, with Trend Micro\'s lists) and gives '
    + 'one total per app for 24 hours, not per hour. Over 24 hours the apps are the router\'s newest such total, for the 24 '
    + 'hours up to the time shown. Over 7 and 30 days they are the sum of the whole days (midnight to midnight) up to the last '
    + 'midnight: Pengumon keeps the router\'s count of each day when it reads it just after midnight, so a day it was not '
    + 'running at that midnight (or any day before Pengumon 0.27) has no app figures, so in the first days after an upgrade '
    + 'the apps cover fewer days than the range. "mostly ..." on a device means one recognized app made up at least 80 % of its traffic over the same hours or '
    + 'days. "General" and similar names are what the router could not recognize, mostly encrypted browsing.'
});
