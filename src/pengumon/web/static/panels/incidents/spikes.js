// The brief latency spikes of the timeline's range (GET /api/latency-spikes): episodes of slow or jittery probes with no loss
// that ended before they became incidents (docs/api.md, "Latency spikes"). One request per call of loadSpikes, which model.js
// makes when the range changes, the incidents_version moves (a new spike bumps it), a Save changes the rules or the zone moves.
// An answer for another range than the card shows now is not used (spikeData): the block waits for its own.

import { zoneParams } from '../../js/clock.js';
import { stripBars, stripStyle } from '../../js/spike-strip.js';

let data = null; // { hours, body }
let sink = () => {};
let seq = 0;

// Where an answer goes: called after every answer that is still wanted.
export function bindSpikes(onChange) { sink = onChange; }

// The last answer, when it is for a window of `hours`; else null.
export const spikeData = (hours) => (data && data.hours === hours ? data.body : null);

// Reads the last `hours` hours; a failure keeps what is shown (the block is an extra: the card works without it). A new
// window (a range) is read at once; the same window again (incidents_version moves every few seconds on a line that flaps) at
// most once in MIN_GAP, the last call kept: the report routes are rate limited (60 answers a minute for all of them).
const MIN_GAP = 30000;
let last = { hours: null, at: 0 };
let timer = null;
export function loadSpikes(hours) {
  clearTimeout(timer);
  timer = null;
  const wait = last.hours === hours ? last.at + MIN_GAP - Date.now() : 0;
  if (wait > 0) { timer = setTimeout(() => { timer = null; loadSpikes(hours); }, wait); return Promise.resolve(); }
  last = { hours, at: Date.now() };
  return read(hours);
}

async function read(hours) {
  const mine = ++seq;
  try {
    const res = await fetch(`/api/latency-spikes?hours=${hours}&${zoneParams()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (mine === seq) { data = { hours, body }; sink(); }
  } catch (err) {
    if (mine === seq) console.warn(`Latency spikes could not be read: ${err.message}`);
  }
}

// ---------- the view model ----------

// Where the spikes were, as a phrase for "mostly ...": the server's stages (reports/spikes.py STAGE_WORDS).
const STAGE_PHRASE = {
  router: 'at the home router', gateway: 'at the gateway', line: 'on the line', isp: 'at the ISP hops',
  internet: 'at the internet servers', load: 'while the line was busy', unknown: 'at an unknown place'
};

// "2 min", "90 s": the wait as the setting is in force.
export function afterWords(seconds) {
  const s = Number(seconds);
  return s >= 60 && s % 60 === 0 ? `${s / 60} min` : `${Number(s.toFixed(1))} s`; // (as the server words it: reports/spikes.py after_words)
}

const dayText = (d) => {
  const at = new Date(`${d}T12:00:00Z`);
  const part = (o) => at.toLocaleDateString('en-US', { ...o, timeZone: 'UTC' });
  return `${part({ weekday: 'short' })} ${part({ month: 'short', day: 'numeric' })}`;
};
// The block of the Incidents card from GET /api/latency-spikes: null when there is nothing to say (no answer yet, or the rule
// "a latency incident at once" is set: nothing is a spike then), a quiet line for a range without spikes, else
// { title, summary, where, style, bars, aria, quiet }.
export function present(body) {
  if (!body || !(body.incident_after_seconds > 0)) return null;
  const title = `Latency spikes (no loss, < ${afterWords(body.incident_after_seconds)}):`;
  if (!(body.count > 0)) return { quiet: true, title, summary: 'No latency spikes in this range', where: '', style: '', bars: [], aria: '' };
  const span = Math.max(1, Math.round((body.window_end - body.window_start) / 86400));
  const today = body.days.length ? body.days[body.days.length - 1].count : 0;
  const top = body.by_stage[0];
  const share = top ? top.count / body.count : 0;
  const lead = share >= 1 ? 'all' : share > 0.5 ? 'mostly' : 'most often';
  const cells = body.days.map((d) => ({ id: d.day, name: dayText(d.day), count: d.count, hours: d.hours, cut: !!d.partial }));
  const summary = `${body.count} in ${span} days · ${today} today`;
  return {
    quiet: false, title, summary,
    where: top ? `${lead} ${STAGE_PHRASE[top.stage] || top.stage}` : '',
    style: stripStyle(cells), bars: stripBars(cells),
    aria: `${title} ${summary}`
  };
}
