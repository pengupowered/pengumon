// Help texts of this panel and its dialog (help.js shows them in the popover).

import { registerHelp } from '../../js/help.js';

registerHelp({
  'traffic.use': 'How much the household used the internet, as your router counted it in its own Traffic Analyzer: download '
    + '(what came in) and upload (what went out) for each clock hour (for each calendar day over 30 days), which devices used '
    + 'the most, and which apps. Every range counts back from now: the last 24, 168 or 720 hours up to the newest hour the '
    + 'router has published, so a longer range always contains the shorter one. Over 30 days the first and last bar are the '
    + 'days the range cuts: they are narrower and count only the hours they hold. The router '
    + 'publishes an hour a few minutes after it ends, and Pengumon reads it about once an hour, so the newest hour is '
    + '"not published yet" for a while: that is not zero use, only no figure. An hour Pengumon was not running for is '
    + '"not stored". Use it to see what filled the line during a slowdown: the incident inspector names the devices of the '
    + 'hours it covers. These are bytes at the router\'s internet port, in decimal units (1 GB = 1,000 MB).',
  'traffic.apps': 'The router sorts what it sees into apps (by their servers and ports, with Trend Micro\'s lists) and gives '
    + 'one total per app for 24 hours, not per hour. Pengumon adds up the router\'s 24-hour counts that lie wholly inside '
    + 'the range, newest first and never overlapping: over 24 hours that is its newest count. What the counts do not '
    + 'cover, for example the hours before Pengumon began keeping them, or a day it was not running, is shown as "Not broken '
    + 'down by app": the device\'s or the network\'s total over the range minus the apps listed, so the list always adds '
    + 'up to the total. "mostly ..." on a device means one recognized app made up at least 80 % of its traffic over the hours '
    + 'the apps were counted for. "General" and similar names are what the router could not recognize, mostly encrypted browsing.'
});
