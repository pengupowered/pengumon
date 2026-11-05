// Incident inspector: what the server's analysis of one incident (GET /api/incidents/{id}/inspect) says, laid out as the
// verdict, the probe results per hop, a short timeline, the tracks on one time axis (inspector-charts.js), the evidence,
// the hop figures and the path trace. Every figure and grade is the server's; this file only arranges them.

import { registerActions } from '../../js/dispatch.js';
import { closeModal, escapeHtml, formatClock, getTargetInfo, icon, keepKeyboard, openModal, setBadge, sortTargetsByCanonical, STATUS, targetList } from '../../js/util.js';
import { shortLabels } from '../../js/path.js';
import { bus } from '../../js/bus.js';
import { primaryDevice } from '../../js/devices.js';
import { causeOf, durationOf, formatDuration, levelOfIncident, loadChip, whenText } from './incidents.js';
import { renderTracks } from './inspector-charts.js';

const LANE_CELLS = 90;
const $ = (id) => document.getElementById(id);
const has = (v) => v !== null && v !== undefined;
const clock = (ts, seconds = false) => formatClock(ts * 1000, false, seconds);
const sign = (s) => `${s < 0 ? '−' : '+'}${formatDuration(Math.abs(Math.round(s)))}`;
const isLost = (s) => s.success === 0 || (s.packet_loss_pct || 0) > 0 || s.latency_ms === null;
// A hop lost probes when its counts say so: loss_pct is rounded to 0.1 %, and one lost probe in thousands rounds to 0.
const lostOf = (h) => Math.max(0, h.sent - h.received);
const lossText = (h) => (lostOf(h) > 0 && h.loss_pct < 0.1 ? '<0.1%' : `${h.loss_pct}%`);
let lastWindow = null;

// A target as the rest of the page names it (Router, Gateway, ISP hop N, Internet ...: path.js shortLabels), its configured
// name in the tooltip. Refreshed per inspection, so a target added since the page loaded is named too.
let labels = {};
const shortOf = (tid) => labels[tid] || getTargetInfo(tid).name;
const fullOf = (tid) => getTargetInfo(tid).name;

// ---------- Verdict ----------
function renderHead(inc, hops) {
  const level = levelOfIncident(inc);
  const active = !has(inc.end_time);
  const word = active ? 'Ongoing' : inc.expected ? 'Expected' : level === 'crit' ? 'Outage' : 'Degraded';
  const dur = durationOf(inc);
  $('inspDiagnosisCard').className = `insp-head st-${level}`;
  setBadge($('inspBadge'), level, word);
  $('inspCauseTitle').textContent = inc.title;
  $('inspCauseDesc').textContent = inc.description;
  const lossy = hops.filter((h) => lostOf(h) > 0);
  const path = lossy.length ? `${lossy[0].short}${lossy.length > 1 ? ` → ${lossy[lossy.length - 1].short}` : ''}` : '';
  const pathTitle = lossy.length ? `${lossy[0].name}${lossy.length > 1 ? ` → ${lossy[lossy.length - 1].name}` : ''}` : '';
  $('inspChips').innerHTML = [
    `<span class="chip">${icon('clock')}${escapeHtml(whenText(inc.start_time))}</span>`,
    `<span class="chip" id="inspDuration"${dur.title ? ` title="${escapeHtml(dur.title)}"` : ''}>${icon('history')}${escapeHtml(active ? 'in progress' : dur.long)}</span>`,
    path ? `<span class="chip" title="${escapeHtml(pathTitle)}">${icon('path')}${escapeHtml(path)}</span>` : '',
    `<span class="chip">Cause: ${escapeHtml(causeOf(inc))}</span>`,
    inc.expected ? '<span class="chip st st-busy">busy line</span>' : '',
    loadChip(inc).trim()
  ].join('');
  $('inspTimeWindowLabel').textContent = `${whenText(inc.start_time)} · ${active ? 'in progress' : dur.long}`;
  $('inspModalTitle').textContent = 'Incident';
}

// ---------- Where it happened: one lane per hop, cells across the window ----------
function renderLanes(data, inc) {
  const ws = data.window_start;
  const span = Math.max(1, data.window_end - ws);
  // A cell is at least as wide as the widest step between one target's samples: a timeout delays the next probe round
  // (about 1.3 s apart during an outage), and narrower cells would fall between two probes and read "no probes" right
  // where the loss is. Steps over 5 s are a real absence (a restart) and do not widen the cells.
  const step = data.sample_bucket_seconds || 1;
  const seen = new Map();
  let widest = step;
  [...data.samples].sort((a, b) => a.timestamp - b.timestamp).forEach((s) => {
    const prev = seen.get(s.target_id);
    const gap = prev === undefined ? 0 : s.timestamp - prev;
    if (gap > widest && gap <= 5 * step) widest = gap;
    seen.set(s.target_id, s.timestamp);
  });
  const cells = Math.max(10, Math.min(LANE_CELLS, Math.floor(span / widest)));
  const byTarget = new Map();
  data.samples.forEach((s) => {
    if (!byTarget.has(s.target_id)) byTarget.set(s.target_id, Array.from({ length: cells }, () => ({ n: 0, lost: 0, full: 0 })));
    const bin = byTarget.get(s.target_id)[Math.min(cells - 1, Math.max(0, Math.floor(((s.timestamp - ws) / span) * cells)))];
    bin.n += 1;
    if (isLost(s)) { bin.lost += 1; if (s.success === 0 || (s.packet_loss_pct || 0) >= 100) bin.full += 1; }
  });
  const rows = sortTargetsByCanonical(byTarget.keys()).map((tid) => {
    const name = shortOf(tid);
    const strip = byTarget.get(tid).map((b, i) => {
      const t = ws + ((i + 0.5) / cells) * span;
      const during = t >= data.event_start && t <= data.event_end;
      const word = b.n === 0 ? 'no probes' : b.lost === 0 ? (during && inc.expected ? 'busy line' : 'answered') : b.full === b.n ? 'no replies' : 'some probes lost';
      const cls = b.n === 0 ? 'n' : b.lost === 0 ? (during && inc.expected ? 'b' : '') : b.full === b.n ? 'c' : 'w';
      return `<i${cls ? ` class="${cls}"` : ''} data-tip="${escapeHtml(`${name} · ${clock(t, true)} · ${word}`)}"></i>`;
    }).join('');
    return `<span class="lane-n" title="${escapeHtml(fullOf(tid))}">${escapeHtml(name)}</span><div class="strip" style="--n:${cells}">${strip}</div>`;
  }).join('');
  const at = (t) => `${(((t - ws) / span) * 100).toFixed(1)}%`;
  const before = Math.round(data.event_start - ws);
  const after = Math.round(data.window_end - data.event_end);
  $('inspLanes').innerHTML = `${rows}<span></span><div class="lane-axis"><span style="left:0">${sign(-before)}</span><span style="left:${at(data.event_start)}">start</span><span style="left:${at(data.event_end)}">end</span><span style="left:100%">${sign(after)}</span></div>`;
  $('inspLanesNote').textContent = `per hop, ${formatDuration(before)} before to ${formatDuration(after)} after`;
}

// ---------- What happened ----------
function renderEvents(data, inc, hops) {
  const lost = data.samples.filter(isLost).sort((a, b) => a.timestamp - b.timestamp);
  const t0 = data.event_start;
  const events = [[data.window_start - t0, lost.length && lost[0].timestamp < t0 - 1
    ? `Window opens: loss already present at ${shortOf(lost[0].target_id)}.` : 'Window opens: all hops clean.']];
  const first = lost.find((s) => s.timestamp >= t0 - 1);
  events.push(first
    ? [first.timestamp - t0, `First lost probes at ${shortOf(first.target_id)}.`]
    : [0, 'No probe was lost in this window: the incident was raised on latency.']);
  const corr = data.docsis_correlation;
  const snap = corr && corr.nearest_snapshot;
  const snapIn = snap && (data.docsis_snapshots || []).some((s) => s.timestamp === snap.timestamp);
  if (corr && corr.gateway_offline) events.push([0, 'The modem stopped answering its polls.']);
  else if (corr && snapIn && corr.active_errors_in_window > 0) events.push([snap.timestamp - t0, `The modem reports ${corr.active_errors_in_window.toLocaleString()} new uncorrectable errors.`]);
  const worst = hops.filter((h) => lostOf(h) > 0).sort((a, b) => b.loss_pct - a.loss_pct || lostOf(b) - lostOf(a))[0];
  if (worst) events.push([Math.max(0, (data.event_end - t0) / 2), `Worst loss: ${lossText(worst)} at ${worst.short}.`]);
  events.push([data.event_end - t0, has(inc.end_time) ? 'Probes answer again; the incident closes.' : 'Not over yet.']);
  events.sort((a, b) => a[0] - b[0]);
  $('inspEvents').innerHTML = events.map(([s, text]) => `<li><span class="ev-t num">${Math.abs(s) < 1 ? '0 s' : sign(s)}</span><span>${escapeHtml(text)}</span></li>`).join('');
}

// ---------- Verdict and evidence ----------
function renderEvidence(data, inc, hops) {
  const items = [];
  const home = hops.filter((h) => h.category === 'local');
  home.forEach((h) => items.push(!h.sent ? ['unknown', h.short, 'no probes in the window', h.name]
    : [!lostOf(h) ? 'good' : h.received === 0 ? 'crit' : 'warn', h.short,
      !lostOf(h) ? `0 lost of ${h.sent} probes` : `${lossText(h)} lost (${lostOf(h)} of ${h.sent} probes)`, h.name]));
  const corr = data.docsis_correlation;
  if (corr) {
    const snap = corr.nearest_snapshot;
    const inWindow = snap && (data.docsis_snapshots || []).some((s) => s.timestamp === snap.timestamp);
    items.push(corr.gateway_offline ? ['crit', 'Gateway', 'the modem did not answer during the incident']
      : !snap || !inWindow ? ['unknown', 'Line', 'no modem reading inside the window']
        // at the gateway's home-side address the line's errors are a finding of their own, not the incident's cause
        : corr.rf_correlated ? [inc.cause === 'gateway' ? 'warn' : 'crit', 'Line', `uncorrectable errors burst (+${(corr.active_errors_in_window || 0).toLocaleString()})`]
          : ['good', 'Line', 'no uncorrectable errors during the window']);
  }
  items.push(inc.load_suspect ? ['unknown', 'Line load', 'load: not a valid figure']
    : has(inc.load_pct) ? [inc.expected ? 'busy' : 'good', 'Line load', `${Math.round(inc.load_pct)}% of capacity at the peak`]
      : ['unknown', 'Line load', 'unknown: no router reports WAN traffic']);
  const firstLoss = hops.find((h) => lostOf(h) > 0);
  items.push(firstLoss ? [inc.severity === 'critical' ? 'crit' : 'warn', 'First hop with loss', `${firstLoss.short} (${lossText(firstLoss)})`, firstLoss.name]
    : ['good', 'First hop with loss', 'none: no probe was lost']);
  $('inspEvidence').innerHTML = items.map(([lv, k, v, title]) => `<li class="st-${lv}"${title ? ` title="${escapeHtml(title)}"` : ''}>${icon(STATUS[lv].icon)}<b>${escapeHtml(k)}</b><span>${escapeHtml(v)}</span></li>`).join('');
}

// ---------- Hop figures and path trace ----------
function renderHopTable(hops) {
  $('inspHopTableBody').innerHTML = hops.map((h) => `<tr>
    <td class="lead"><span class="primary" title="${escapeHtml(h.name)}"><i class="key" style="--c:${escapeHtml(getTargetInfo(h.target_id).color)}"></i> ${escapeHtml(h.short)}</span></td>
    <td class="aside"><span class="chip">${escapeHtml(h.category)}</span></td>
    <td data-l="Loss" class="r num ${h.received === 0 && h.sent > 0 ? 'ink-crit' : lostOf(h) > 0 ? 'ink-warn' : ''}"><b>${h.sent ? lossText(h) : '-'}</b></td>
    <td data-l="Avg" class="r num">${has(h.avg_ms) ? `${h.avg_ms} ms` : '-'}</td>
    <td data-l="Peak" class="r num">${has(h.max_ms) ? `${h.max_ms} ms` : '-'}</td>
    <td data-l="Jitter" class="r num hide-narrow">${has(h.jitter_ms) ? `${h.jitter_ms} ms` : '-'}</td>
    <td data-l="Received / sent" class="r num hide-narrow ink-muted">${h.received} / ${h.sent}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">No hop figures for this incident.</td></tr>';
}

function renderTrace(inc) {
  const trace = inc.path_trace && inc.path_trace.length ? inc.path_trace : null;
  $('inspPathTraceContainer').hidden = !trace;
  if (!trace) return;
  $('inspPathTraceBody').innerHTML = trace.map((h) => `<tr>
    <td class="lead num"><b>${h.hop}</b></td>
    <td class="mono aside"><code>${escapeHtml(h.ip)}</code></td>
    <td data-l="">${escapeHtml(h.hostname || '-')}</td>
    <td class="r num" data-l="RTT">${has(h.latency_ms) ? `${h.latency_ms} ms` : '<span class="lost">no reply</span>'}</td></tr>`).join('');
}

// ---------- The line during the incident (the modem's reading next to the incident) ----------
// What the line's errors, or their absence, say about where the incident sat (its cause category, incidents.taxonomy).
// The Gateway hop is the modem's home-side address, which does not use the line: errors on the line do not explain
// loss there unless the modem restarted. Loss from the provider's first router on (Line) with errors at the same time
// points to the line itself; without them it is the line or that router.
const ERRORS_MEAN = {
  gateway: 'The loss started at the gateway\'s home-side address, which does not use the line, so these errors do not explain it unless the modem restarted; they are worth a line check of their own.',
  line: 'The loss started at the provider\'s first router while the gateway answered; errors at the same time point to your cable line itself.'
};
const CLEAN_MEANS = {
  gateway: 'The loss sat at the gateway itself (its home-side address), not on the line.',
  line: 'The loss started past the modem: the provider\'s first router, or the line in a way these counters do not show.'
};
function renderLine(data) {
  const cause = data.incident && data.incident.cause;
  const card = $('inspDocsisCard');
  const corr = data.docsis_correlation;
  card.hidden = !corr;
  if (!corr) return;
  const snap = corr.nearest_snapshot;
  const inWindow = snap && (data.docsis_snapshots || []).some((s) => s.timestamp === snap.timestamp);
  const outside = Boolean(snap) && !inWindow;
  let level = 'good';
  let word = 'Clean';
  let html;
  if (corr.gateway_offline) {
    [level, word] = ['crit', 'Unreachable'];
    const line = primaryDevice('access_line');
    html = `<strong>The modem did not answer:</strong> the device at ${escapeHtml(line ? line.host : 'the modem')} did not respond during this incident (a freeze, a crash or a reboot). Its readings were interrupted while it was down.`;
  } else if (outside) {
    [level, word] = ['unknown', 'Nearby reading'];
    const gap = snap.timestamp < data.window_start ? data.window_start - snap.timestamp : snap.timestamp - data.window_end;
    html = `<strong>Modem reading from ${clock(snap.timestamp)}, ${Math.max(1, Math.round(gap / 60))} min outside the incident:</strong> no modem poll fell inside this window, so this reading is context only and says nothing for or against a line problem during the incident.`;
  } else if (!snap) {
    [level, word] = ['unknown', 'No reading'];
    html = '<strong>No modem reading for this incident:</strong> no modem poll was recorded in or near this window, so the line cannot be judged either way.';
  } else if (corr.rf_correlated) {
    [level, word] = [cause === 'gateway' ? 'warn' : 'crit', 'Errors during the incident'];
    html = `<strong>Uncorrectable errors on the line:</strong> ${(corr.active_errors_in_window || 0).toLocaleString()} uncorrectable codewords were counted during this incident. ${escapeHtml(ERRORS_MEAN[cause] || 'Noise on the cable plant fits this packet loss.')}`;
  } else {
    html = `<strong>The line was clean:</strong> no uncorrectable errors were counted during this incident${has(snap.downstream_snr_avg) ? `, and the downstream SNR was ${snap.downstream_snr_avg} dB` : ''}. ${escapeHtml(CLEAN_MEANS[cause] || 'The loss lies beyond the modem.')}`;
  }
  setBadge($('inspDocsisBadge'), level, word);
  $('inspDocsisDesc').innerHTML = html;
  const set = (id, text, grade) => { const el = $(id); el.textContent = text; el.className = grade === 'critical' ? 'ink-crit' : grade === 'warn' ? 'ink-warn' : grade === 'good' ? 'ink-good' : 'ink-muted'; };
  const value = (v, unit) => (has(v) ? `${(+v).toFixed(1)} ${unit}` : '-');
  if (snap) {
    $('inspDocsisTime').textContent = `Modem poll: ${clock(snap.timestamp, true)}`;
    set('inspDocsisDsPower', value(snap.downstream_power_avg, 'dBmV'), snap.grades && snap.grades.ds_power);
    set('inspDocsisUsPower', value(snap.upstream_power_avg, 'dBmV'), snap.grades && snap.grades.us_power);
    const w = corr.worst_channel;
    if (w && has(w.channel_id)) {
      $('inspDocsisWorstLabel').textContent = `Ch ${w.channel_id}${w.frequency_mhz ? ` (${Math.round(w.frequency_mhz)} MHz)` : ''} SNR`;
      const delta = w.uncorrectables_recent > 0 ? ` (+${w.uncorrectables_recent.toLocaleString()} new)` : w.uncorrectables > 0 ? ` (${w.uncorrectables.toLocaleString()} total)` : '';
      set('inspDocsisWorstVal', `${value(w.snr_db, 'dB')}${delta}`, w.grades && w.grades.snr);
    } else {
      $('inspDocsisWorstLabel').textContent = 'Worst channel SNR';
      set('inspDocsisWorstVal', '-');
    }
  } else {
    $('inspDocsisTime').textContent = 'No modem reading';
    ['inspDocsisDsPower', 'inspDocsisUsPower', 'inspDocsisWorstVal'].forEach((id) => set(id, '-'));
  }
  set('inspDocsisStatusVal', corr.gateway_offline ? 'Unreachable' : !snap || outside ? 'Unknown' : 'Answering', corr.gateway_offline ? 'critical' : !snap || outside ? null : 'good');
  if (!snap || outside) set('inspDocsisFecVal', '-');
  else if (corr.active_errors_in_window > 0) set('inspDocsisFecVal', `+${corr.active_errors_in_window.toLocaleString()} errors`, 'critical');
  else set('inspDocsisFecVal', '0 errors (clean)', 'good');
}

export async function inspectIncident(incidentId) {
  openModal('incidentInspectorModal');
  document.querySelector('#incidentInspectorModal .m-close').focus(); // keyboard users land inside the dialog
  $('inspCauseTitle').textContent = 'Loading incident details...';
  $('inspCauseDesc').textContent = '';
  $('inspChips').innerHTML = '';
  $('inspLanes').innerHTML = '';
  $('inspEvents').innerHTML = '';
  $('inspEvidence').innerHTML = '';
  $('inspHopTableBody').innerHTML = '<tr><td colspan="7" class="muted">Loading hop impact...</td></tr>';
  $('btnInspAskAi').dataset.arg = String(incidentId); // Ask AI covers this incident
  try {
    const res = await fetch(`/api/incidents/${incidentId}/inspect`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const inc = data.incident;
    const minute = data.hop_resolution === 'minute';
    const note = $('inspHopResolution');
    note.hidden = !minute;
    note.textContent = minute
      ? 'Minute resolution: the raw samples of this incident are past retention, so these figures are averaged over whole minutes from the per-minute rollups (a short drop reads lower than it was).'
      : '';
    const byId = new Map((data.hop_impact || []).map((h) => [h.target_id, h]));
    labels = shortLabels(targetList());
    // name: the configured name (tooltips); short: the page's label for it (what the inspector shows)
    const hops = sortTargetsByCanonical(byId.keys()).map((tid) => {
      const name = getTargetInfo(tid).name || byId.get(tid).name;
      return { ...byId.get(tid), name, short: labels[tid] || name };
    });
    renderHead(inc, hops);
    renderLanes(data, inc);
    renderEvents(data, inc, hops);
    renderEvidence(data, inc, hops);
    renderHopTable(hops);
    renderTrace(inc);
    renderLine(data);
    lastWindow = { start: data.window_start, end: data.window_end };
    setTimeout(() => renderTracks(data), 60); // after the dialog has its size
  } catch (err) {
    $('inspCauseTitle').textContent = 'Failed to Load Incident';
    $('inspCauseDesc').textContent = err.message;
  }
}

// "Show in latency chart": the main chart loads the incident's window.
function showInChart() {
  if (!lastWindow) return;
  closeModal('incidentInspectorModal');
  bus.publish('chart:range', { start: lastWindow.start, end: lastWindow.end }); // the latency panel shows it
}

// Escape and Tab; the dossier on top of the inspector is the topmost dialog and closes first.
keepKeyboard(document.getElementById('incidentInspectorModal'));

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  inspectIncident: (el) => inspectIncident(Number(el.dataset.arg)),
  showIncidentInChart: () => showInChart()
});
