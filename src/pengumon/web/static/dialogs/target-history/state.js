// The target history dialog's shared state: the ranges, and what the dialog shows now (one target or a group, the view,
// the range, the newest request, the two charts). Module-level `let`s cannot be assigned from another module, so the
// values that change live as properties of one object.

import { localFormat } from '../../js/clock.js';
import { longBucket, rangeOptions, retentionDays } from '../../js/ranges.js';

export const LIVE = 300; // the dialog's Live: the last 5 minutes
// The ranges on offer (js/ranges.js: the probes' retention decides 90 d and All): [seconds, label, bucket seconds, id,
// words, all]. Live is the last 5 minutes here.
export const bucketOf = (seconds) => (seconds <= LIVE ? 2 : seconds <= 900 ? 10 : seconds <= 3600 ? 30 : seconds <= 86400 ? 600
  : seconds <= 604800 ? 900 : seconds <= 2592000 ? 3600 : longBucket(seconds)); // (400-800 points a line from 7 d on)
export const rangeList = () => rangeOptions({ days: retentionDays('probes'), min: '1h', live: true, quarter: true })
  .map((o) => [o.id === 'live' ? LIVE : o.seconds, o.short, bucketOf(o.id === 'live' ? LIVE : o.seconds), o.id, o.words, o.all]);
// What each range's figures cover, said in their labels (a figure without its window cannot be compared with the path's).
const WINDOW = { 300: 'last 5 min', 900: 'last 15 min', 3600: 'last hour', 86400: 'last 24 h', 604800: 'last 7 days', 2592000: 'last 30 days', 7776000: 'last 90 days' };
export const win = () => (T.since ? `since ${localFormat(T.since * 1000, { month: 'short', day: 'numeric' })}` : null) || WINDOW[T.rangeSeconds] || (T.rangeSeconds % 86400 === 0 ? `all ${T.rangeSeconds / 86400} days` : 'last 24 h');
export const labels = () => ['Status, latest 15 min', `Median, ${win()}`, `p90 peak, ${win()}`, `Loss, ${win()}`, 'Available, 24 h'];

export const T = {
  targetId: null, // the target of a single-target dialog
  group: null, // {title, rule: 'median' | 'last', category, members: [{id, short, name}]} of a dialog opened on a group
  view: 'all', // with a group: 'all' or the id of the member shown
  rangeSeconds: 3600,
  // what the charts draw now (All starts at the oldest stored probe, not at the retention): the start (epoch seconds), the span,
  // the bucket width, and where All starts when that is the oldest stored row (else null)
  start: null,
  spanSeconds: 3600,
  bucket: 30,
  since: null,
  seq: 0, // the newest load; an answer for an older one is dropped
  latChart: null,
  lossChart: null
};
