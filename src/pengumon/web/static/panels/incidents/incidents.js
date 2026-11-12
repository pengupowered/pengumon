// What an incident is called and how long it lasted, as words: the helpers of the incident list (model.js), the timeline and
// the inspector (inspector.js).

import { localFormat } from '../../js/clock.js';
import { CAUSE_SHORT } from '../../js/roles.js';

// Where an incident's fault sat, as a word (js/path-api.js CAUSES, in the timeline's lane order).
export const causeOf = (inc) => CAUSE_SHORT[inc.cause] || 'Other';

export function levelOfIncident(inc) {
  if (inc.expected) return 'busy';
  return inc.severity === 'critical' ? 'crit' : 'warn';
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(Math.round(seconds % 60)).padStart(2, '0')}s`;
  return `${Math.floor(seconds / 3600)}h ${String(Math.round((seconds % 3600) / 60)).padStart(2, '0')}m`;
}

// How long an incident lasted, as people felt it: the loss span (the server's loss_seconds, first to last lost probe plus
// one probe interval) when probes were lost, else the recorded duration. The recorded one runs from detection to
// recovery, a few seconds after the loss on both ends; it is the hover text, and the inspector shows both. Old
// incidents (no samples to read the loss from) and those raised on latency (nothing lost) show the recorded duration.
// {text: the short form, long: "Loss 7s · incident 12s", title: the explanation, lost: whether a loss span is shown}.
export function durationOf(inc) {
  const open = inc.duration_seconds === null || inc.duration_seconds === undefined;
  if (open) return { text: 'active', long: 'in progress', title: '', lost: false };
  const recorded = formatDuration(inc.duration_seconds);
  if (!(inc.loss_seconds > 0)) {
    const why = inc.loss_seconds === 0 ? 'no probe was lost (raised on latency)' : 'no stored probes to read the loss from';
    return { text: recorded, long: recorded, title: `Incident open ${recorded}, from detection to recovery; ${why}`, lost: false };
  }
  const lost = formatDuration(inc.loss_seconds);
  return { text: `${lost} lost`, long: `Loss ${lost} · incident ${recorded}`, lost: true,
    title: `Probes lost for ${lost} (first to last lost probe). The incident was open ${recorded}: it is detected a few seconds after the loss starts and closes a few seconds after it ends.` };
}

export function whenText(ts) {
  const ms = ts * 1000;
  const time = localFormat(ms, { hour: '2-digit', minute: '2-digit' });
  const day = { year: 'numeric', month: 'numeric', day: 'numeric' };
  if (localFormat(ms, day) === localFormat(Date.now(), day)) return `Today ${time}`;
  if (Date.now() - ms < 6 * 86400000) return `${localFormat(ms, { weekday: 'short' })} ${time}`;
  return `${localFormat(ms, { month: 'short', day: 'numeric' })} ${time}`;
}

// The load figure of an incident as a chip: the peak WAN load, or the words saying the recorded figure is unusable.
export function loadChip(inc) {
  return inc.load_pct === null || inc.load_pct === undefined ? ''
    : inc.load_suspect ? ' <span class="chip" title="The WAN rate recorded with this incident was impossible for the line; the figure is not used">load: not a valid figure</span>'
      : ` <span class="chip" title="Peak WAN load around this event, % of line capacity">${Math.round(inc.load_pct)}% load</span>`;
}
