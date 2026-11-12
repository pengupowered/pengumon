// A timer that does nothing while the tab is hidden and catches up when it shows again: `fn` runs every `ms` while the
// page is visible, and once on the way back if a run was skipped. Panels use it for the refreshes and redraws nobody
// would see in a hidden tab (a timer there is also what keeps a background tab from sleeping).

export function everyWhileVisible(fn, ms) {
  let skipped = false;
  const run = () => {
    skipped = false;
    fn();
  };
  const timer = setInterval(() => {
    if (document.hidden) skipped = true;
    else run();
  }, ms);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && skipped) run(); });
  return timer;
}

// Runs `fn` now, or when the tab next shows if it is hidden (a poll that came due in a hidden tab).
export function whenVisible(fn) {
  if (!document.hidden) { fn(); return; }
  const on = () => {
    if (document.hidden) return;
    document.removeEventListener('visibilitychange', on);
    fn();
  };
  document.addEventListener('visibilitychange', on);
}
