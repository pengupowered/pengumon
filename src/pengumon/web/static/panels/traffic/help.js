// Help texts of this panel and its dialog (help.js shows them in the popover).

import { registerHelp } from '../../js/help.js';

registerHelp({
  'traffic.use': 'How much the household used the internet, as your router counted it in its own Traffic Analyzer: download '
    + '(what came in) and upload (what went out) for each clock hour, which devices used the most, and which apps. The top of the '
    + 'card is one hour, the newest by default: its download and upload, their average speed over the hour and the devices that '
    + 'used it most. Click the chart (or use ‹ ›) to pick another hour: the top shows it, and the devices below are that '
    + 'hour\'s (there are no apps for one hour: the router counts them per 24 hours); Whole range goes back. The chart is a '
    + 'point per hour up to 90 days, a point per calendar day past that. Every range counts back from now: the last 1, 24, '
    + '168 or 720 hours up to the newest hour the router has published, so a longer range always contains the shorter one; '
    + 'until an hour is picked, the lists below the chart cover the whole range. The router publishes an hour a few minutes after it ends, and Pengumon reads it about '
    + 'once an hour, so the newest hour can be an hour old, and an hour not published yet is "not published yet": that is '
    + 'not zero use, only no figure. An hour Pengumon was not running for is "not stored". The live speed is on the Latency '
    + 'chart. These are bytes at the router\'s internet port, in decimal units (1 GB = 1,000 MB).',
  'traffic.apps': 'The router sorts what it sees into apps (by their servers and ports, with Trend Micro\'s lists) and gives '
    + 'one total per app for 24 hours, not per hour. Pengumon adds up the router\'s 24-hour counts that lie wholly inside '
    + 'the range, newest first and never overlapping: over 24 hours that is its newest count. What the counts do not '
    + 'cover, for example the hours before Pengumon began keeping them, or a day it was not running, is shown as "Not broken '
    + 'down by app": the device\'s or the network\'s total over the range minus the apps listed, so the list always adds '
    + 'up to the total. "mostly ..." on a device means one recognized app made up at least 80 % of its traffic over the hours '
    + 'the apps were counted for. "General" and similar names are what the router could not recognize, mostly encrypted browsing.'
});
