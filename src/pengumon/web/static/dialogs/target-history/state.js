// The target history dialog's shared state: the ranges, and what the dialog shows now (one target or a group, the view,
// the range, the newest request, the two charts). Module-level `let`s cannot be assigned from another module, so the
// values that change live as properties of one object.

export const RANGES = [[300, 'Live', 2], [900, '15 m', 10], [3600, '1 h', 30], [86400, '24 h', 600], [604800, '7 d', 3600]]; // seconds, label, bucket seconds
export const LIVE = 300;
// What each range's figures cover, said in their labels (a figure without its window cannot be compared with the path's).
const WINDOW = { 300: 'last 5 min', 900: 'last 15 min', 3600: 'last hour', 86400: 'last 24 h', 604800: 'last 7 days' };
export const win = () => WINDOW[T.rangeSeconds];
export const labels = () => ['Status, latest 15 min', `Median, ${win()}`, `p90 peak, ${win()}`, `Loss, ${win()}`, 'Available, 24 h'];

export const T = {
  targetId: null, // the target of a single-target dialog
  group: null, // {title, rule: 'median' | 'last', category, members: [{id, short, name}]} of a dialog opened on a group
  view: 'all', // with a group: 'all' or the id of the member shown
  rangeSeconds: 3600,
  seq: 0, // the newest load; an answer for an older one is dropped
  latChart: null,
  lossChart: null
};
