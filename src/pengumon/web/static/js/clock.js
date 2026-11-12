// The page's clock: times cut on the zone Settings names (else the browser's), and the zone parameters for requests.

// A clock time (hours and minutes, with seconds on request) in the viewer's locale; with the date for ranges over a day.
// The formatters are made once: toLocaleTimeString with options built a new Intl.DateTimeFormat on every call, and a
// chart relabels hundreds of points on every message.
// The zone Settings names (general.timezone, from /api/info `timezone`), else null: the browser's own. Every time on the
// page is cut on this clock, so a page opened from another zone agrees with the reports and the AI pack. A change (the
// first answer of /api/info, a Save in Settings) is told as a `zonechange` event on the document: the cards that write
// times draw again.
let pageZoneName = null;
let CLOCK = {};
export function setPageZone(name) {
  let zone = name || null;
  if (zone) { try { new Intl.DateTimeFormat([], { timeZone: zone }); } catch { zone = null; } } // a zone this browser cannot name
  if (zone === pageZoneName) return;
  pageZoneName = zone;
  CLOCK = {};
  document.dispatchEvent(new CustomEvent('zonechange'));
}
// Milliseconds -> text in the page's zone, `options` as for Intl.DateTimeFormat (through zonedFormat, below).
// An options object that is used again (a constant) finds its formatter by identity, without making its JSON key each
// time (the live chart formats 60 labels per message).
export function localFormat(ms, options) {
  if (options && typeof options === 'object') {
    const byObject = (CLOCK.byObject ||= new WeakMap());
    let f = byObject.get(options);
    if (!f) {
      const key = JSON.stringify(options);
      f = (CLOCK[key] ||= zonedFormat({ zone: pageZoneName }, options));
      byObject.set(options, f);
    }
    return f(ms / 1000);
  }
  const key = JSON.stringify(options);
  return (CLOCK[key] ||= zonedFormat({ zone: pageZoneName }, options))(ms / 1000);
}
// Milliseconds east of UTC the page's clock is at `ms` (the browser's own when Settings names no zone).
export function zoneOffsetMs(ms) {
  if (!pageZoneName) return -new Date(ms).getTimezoneOffset() * 60000;
  const parts = Object.fromEntries((CLOCK.parts ||= new Intl.DateTimeFormat('en-US', {
    timeZone: pageZoneName, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric'
  })).formatToParts(ms).map((p) => [p.type, +p.value]));
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second) - Math.floor(ms / 1000) * 1000;
}
// The next midnight on the page's clock after `ms`, in epoch milliseconds.
export function nextMidnight(ms) {
  if (!pageZoneName) { const d = new Date(ms); d.setHours(24, 0, 0, 0); return d.getTime(); }
  const DAY_MS = 86400000;
  const guess = Math.floor((ms + zoneOffsetMs(ms)) / DAY_MS) * DAY_MS + DAY_MS; // that midnight as wall-clock time read as UTC
  return guess - zoneOffsetMs(guess - zoneOffsetMs(ms)); // minus the offset in force then (a daylight saving day may differ)
}
const HM = { hour: '2-digit', minute: '2-digit' };
const HMS = { hour: '2-digit', minute: '2-digit', second: '2-digit' };
const MONTH_DAY = { month: 'short', day: 'numeric' };
export function formatClock(ms, withDate = false, seconds = false) {
  const hm = localFormat(ms, seconds ? HMS : HM);
  return withDate ? `${localFormat(ms, MONTH_DAY)} ${hm}` : hm;
}
// Epoch seconds -> text on the clock a report answer was cut on (data.zone: the zone set in Settings, else this
// browser's), which need not be this browser's. A zone this browser cannot name (a fixed offset, in an older one):
// shifted by data.tz_offset_minutes and read as UTC.
export function zonedFormat(data, opts) {
  try {
    const f = new Intl.DateTimeFormat([], { ...opts, timeZone: data.zone || undefined });
    return (ts) => f.format(ts * 1000);
  } catch {
    const f = new Intl.DateTimeFormat([], { ...opts, timeZone: 'UTC' });
    return (ts) => f.format((ts + (data.tz_offset_minutes || 0) * 60) * 1000);
  }
}

// The viewer's time zone for the routes that cut days and hours (/api/report, /api/report/loss-grid, /api/uptime,
// /api/ai-pack): `tz`, the browser's IANA zone, which follows daylight saving, and `tz_offset_minutes`, minutes east of UTC
// now, for a browser without one. The server uses the zone set in Settings first. URLSearchParams: `${zoneParams()}` in a
// URL, or copied into another one.
export function zoneParams() {
  const p = new URLSearchParams({ tz_offset_minutes: String(-new Date().getTimezoneOffset()) });
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zone) p.set('tz', zone);
  } catch { /* no Intl time zones: the offset alone */ }
  return p;
}
