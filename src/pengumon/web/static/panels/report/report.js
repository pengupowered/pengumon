// Reliability report (/api/report).

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { escapeHtml, percentText, targetList } from '../../js/util.js';
import { median, quietKey, quietTargets, shortLabels } from '../../js/path.js';

let shown = null; // the last report and where it went, drawn again when the hops that ignore ping change
let quiet = new Map(); // target id -> its neutral words (path.js quietTargets)

// A hop that ignores ping (path.js quietTargets, from the live `targets` topic) is listed with those words instead of an
// availability that is only its unanswered pings, and never stands in for the internet figure; so is an IPv6 target
// while this host has no IPv6, with its own words.
export function followQuietHops() {
  return bus.subscribe('targets', (targets) => {
    const next = quietTargets(targets);
    if (quietKey(next) === quietKey(quiet)) return;
    quiet = next;
    if (shown && shown.out.isConnected) renderReport(shown.out, shown.report);
  });
}

// Reliability report (/api/report)
export async function loadReport(days) {
  const out = document.getElementById('reportOutput');
  if (!out) return;
  document.querySelectorAll('#reportRangeButtons button').forEach((b) => {
    b.classList.toggle('active', b.dataset.arg === String(days));
    b.setAttribute('aria-pressed', String(b.dataset.arg === String(days)));
  });
  out.innerHTML = '<div class="skel" style="height:44px;width:60%;margin-bottom:14px"></div><div class="skel-stack"><div class="skel line" style="width:92%"></div><div class="skel line" style="width:76%"></div><div class="skel line" style="width:84%"></div></div>';
  try {
    // Day and hour buckets are cut server-side; tell it where this browser's midnight is (minutes east of UTC).
    const tz = -new Date().getTimezoneOffset();
    const res = await fetch(`/api/report?days=${days}&tz_offset_minutes=${tz}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderReport(out, await res.json());
  } catch (err) {
    out.textContent = `Error generating report: ${err.message}`;
  }
}

// The classifier's cause codes in the words the rest of the page uses (Path health: Router, Line, ISP, Internet).
// Display text only; an unknown code is shown as it is.
const CAUSE_TEXT = {
  local_wifi_mesh_drop: 'Router: home network', local_switch_cable_fault: 'Router: cabling',
  double_nat_congestion: 'Line: gateway', access_line_drop: 'ISP: access line', internet_backbone_transit_loss: 'ISP: backbone',
  target_specific_outage: 'Internet: one server', dns_resolver_failure: 'DNS', unknown_outage: 'Unknown',
  load_induced_latency: 'Busy line', service_unavailable: 'Host service'
};
const causeText = (code) => CAUSE_TEXT[code] || code.replace(/_/g, ' ');

// The headline is the internet as seen through the public targets (the median of their availability; the last hop
// when there is no public target); days are bars of what that figure lost (a clean day has no bar, so a bad day stands
// out; bars of 99.9x % all looked full), causes the share of downtime per cause, and a table gives every hop. Nothing is
// coloured by a threshold here: bars are neutral, the numbers say it.
function renderReport(out, report) {
  shown = { out, report };
  const inc = report.incidents;
  const days = report.availability.days;
  const hops = report.availability.hops;
  const fmtPct = (v) => percentText(v, 3);
  const meta = new Map(targetList().map((t) => [t.id, t]));
  const labels = shortLabels(targetList()); // hops as the rest of the page names them, the configured name on hover
  const hopName = (h) => `<span class="primary" title="${escapeHtml(h.name)}">${escapeHtml(labels[h.target_id] || h.name)}</span>`;
  let pub = hops.filter((h) => (meta.get(h.target_id) || {}).category === 'public');
  const heard = hops.filter((h) => !quiet.has(h.target_id));
  if (!pub.length) pub = heard.length ? [heard[heard.length - 1]] : [];
  const avail = median(pub.map((h) => h.availability_pct));
  const perDay = days.map((_, i) => median(pub.map((h) => h.per_day[i])));
  const lostPct = perDay.map((v) => (v === null || v === undefined ? null : Math.max(0, 100 - v)));
  const peak = Math.max(0, ...lostPct.filter((v) => v !== null));
  const peakDay = lostPct.indexOf(peak);
  const minutes = inc.minutes || 0;
  const downtime = minutes >= 60 ? `${Math.floor(minutes / 60)}h ${String(Math.round(minutes % 60)).padStart(2, '0')}m` : `${minutes.toFixed(minutes < 10 ? 1 : 0)} min`;
  const totalMin = inc.by_cause.reduce((a, c) => a + c.minutes, 0) || 1;
  const causes = inc.by_cause.slice(0, 5).map((c) => {
    const share = Math.round((c.minutes / totalMin) * 100);
    return `<li title="${escapeHtml(c.cause_code)}"><span>${escapeHtml(causeText(c.cause_code))}</span><span class="cbar"><i style="width:${share}%"></i></span><b class="num">${share}%</b></li>`;
  }).join('');
  const dayName = (d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: 'short' });
  const dayBars = days.map((d, i) => {
    const v = perDay[i];
    const lost = lostPct[i];
    const h = lost === null || peak <= 0 ? 0 : Math.max(4, (lost / peak) * 100);
    const tip = v === null || v === undefined ? 'no data' : `${fmtPct(v)} answered, ${fmtPct(lost)} lost`;
    return `<div class="day${v === null || v === undefined ? ' nodata' : ''}" data-tip="${escapeHtml(d)} · ${tip}"><i style="height:${h.toFixed(0)}%"></i><span>${escapeHtml(days.length > 10 ? '' : dayName(d).slice(0, 1))}</span></div>`;
  }).join('');
  const daysNote = peak > 0 ? `Worst: ${escapeHtml(dayName(days[peakDay]))} ${escapeHtml(days[peakDay])}, ${fmtPct(peak)} lost` : 'No probe lost on any day';
  const hopRows = hops.map((h) => {
    if (quiet.has(h.target_id)) {
      const q = quiet.get(h.target_id);
      return `<tr><td class="lead">${hopName(h)}</td><td class="r aside muted" title="${escapeHtml(q.why)}">${escapeHtml(q.word)}</td>`
        + '<td class="r num" data-l="Worst day">–</td></tr>';
    }
    let worst = null;
    h.per_day.forEach((v, i) => { if (v !== null && (worst === null || v < worst.v)) worst = { v, day: days[i] }; });
    return `<tr><td class="lead">${hopName(h)}</td><td class="r num aside">${fmtPct(h.availability_pct)}</td>`
      + `<td class="r num" data-l="Worst day">${worst ? `${fmtPct(worst.v)} on ${escapeHtml(worst.day)}` : '–'}</td></tr>`;
  }).join('');
  const excluded = inc.excluded_expected > 0
    ? `<p class="rep-note">${inc.excluded_expected} expected busy-line event${inc.excluded_expected === 1 ? '' : 's'} not counted (latency under heavy load is not a fault).</p>` : '';
  out.innerHTML = `<div class="rep-grid">
      <div class="rep-hero"><div class="stat"><span class="lbl">Availability, last ${report.days} days</span><span class="val hero-num num">${avail === null ? '–' : percentText(avail).slice(0, -1)}<small>%</small></span>
        <span class="sub" title="${escapeHtml(pub.map((h) => h.name).join(', '))}">${pub.length > 1 ? `Internet: median of ${pub.length} public servers` : escapeHtml(pub[0] ? labels[pub[0].target_id] || pub[0].name : 'no probe data yet')}</span></div>
        <div class="rep-mini"><div class="stat"><span class="lbl">Downtime</span><span class="val num">${downtime}</span></div><div class="stat"><span class="lbl">Incidents, ${report.days} d</span><span class="val num">${inc.count}</span></div></div></div>
      <div class="rep-days"><h3 class="rb-t">Lost per day</h3>
        <div class="days" style="grid-template-columns: repeat(${Math.max(1, days.length)}, 1fr)" role="img" aria-label="Share of probes lost per day. ${daysNote}">${dayBars}</div>
        <p class="rep-note">${daysNote}</p></div>
      <div class="rep-causes"><h3 class="rb-t">Downtime by cause</h3><ul class="causes">${causes || '<li class="muted">No incidents.</li>'}</ul></div>
    </div>${excluded}
    <details class="rep-hops-box"><summary class="fine">Every hop</summary>
    <table class="dlist rep-hops"><thead><tr><th>Hop</th><th class="r">Availability</th><th class="r">Worst day</th></tr></thead><tbody>${hopRows || '<tr><td colspan="3" class="muted">No probe data yet.</td></tr>'}</tbody></table></details>`;
  const worst = report.worst_hours[0];
  const hour = (h) => `${String(h).padStart(2, '0')}:00`;
  document.getElementById('reportFoot').textContent = worst ? `Worst hour: ${hour(worst.hour)} (${worst.loss_pct}% loss)` : 'No packet loss recorded.';
  out.dataset.summary = `Last ${report.days} days: ${inc.count} incidents, ${Math.round(minutes)} min`;
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  loadReport: (el) => loadReport(Number(el.dataset.arg))
});
