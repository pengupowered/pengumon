// The DOCSIS adapter of the line panel (panels/line.js): downstream power, downstream SNR and upstream power as range
// gauges (good zone, warning zones, the spread across channels, the value), the new uncorrectable errors in plain words
// with their context, and the channel summary. Its detail view is the Channels dialog (docsis-channels.js). Every
// DOCSIS name and number lives here and in that view; the line panel itself knows no technology.

import { cloneTemplate, escapeHtml, icon, setText, STATUS } from '../../js/util.js';
import { gradeOf, levelOfGrade, rangeOf, worstGrade } from '../../js/grades.js';
import './channels.js'; // the adapter's detail view (registers openChannelsModal)

export const DEFAULT_TITLE = 'Cable modem';
const has = (v) => v !== null && v !== undefined;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const HISTORY = 60; // readings kept for the error bars (since the page opened)

function gaugeBar(range, value, lo, hi) {
  if (!range) return has(value) ? '<div class="g-mark" style="left:50%"></div>' : '';
  const pct = (v) => `${(((clamp(v, range.min, range.max) - range.min) / (range.max - range.min)) * 100).toFixed(2)}%`;
  const width = (a, b) => `calc(${pct(b)} - ${pct(a)})`;
  const zones = [];
  const goodLo = has(range.good_low) ? range.good_low : range.min;
  const goodHi = has(range.good_high) ? range.good_high : range.max;
  const warnLo = has(range.critical_low) ? range.critical_low : range.min;
  const warnHi = has(range.critical_high) ? range.critical_high : range.max;
  if (goodLo > warnLo) zones.push(`<div class="g-zone mid" style="left:${pct(warnLo)};width:${width(warnLo, goodLo)}"></div>`);
  if (warnHi > goodHi) zones.push(`<div class="g-zone mid" style="left:${pct(goodHi)};width:${width(goodHi, warnHi)}"></div>`);
  zones.push(`<div class="g-zone ok" style="left:${pct(goodLo)};width:${width(goodLo, goodHi)}"></div>`);
  if (has(lo) && has(hi) && hi > lo) zones.push(`<div class="g-spread" style="left:${pct(lo)};width:${width(lo, hi)}" title="Spread across channels"></div>`);
  if (has(value)) zones.push(`<div class="g-mark" style="left:${pct(value)}"></div>`);
  return zones.join('');
}

export function goodText(range) {
  if (!range) return '';
  const lo = has(range.good_low) && range.good_low > range.min ? range.good_low : null;
  const hi = has(range.good_high) && range.good_high < range.max ? range.good_high : null;
  if (has(lo) && has(hi)) return `good ${lo} to ${hi}`;
  if (has(lo)) return `good ${lo}+`;
  return has(hi) ? `good up to ${hi}` : 'good';
}

function paintGauge(el, refs, name, range, value, grade, spread) {
  const level = has(value) ? levelOfGrade(grade) : 'unknown';
  el.className = `gauge st-${level}${has(value) ? '' : ' na'}`;
  if (refs.ic.dataset.level !== level) {
    refs.ic.dataset.level = level;
    refs.ic.innerHTML = icon(STATUS[level].icon, 'sm');
  }
  setText(refs.val, has(value) ? (+value).toFixed(1) : '–');
  refs.bar.innerHTML = gaugeBar(range, value, spread && spread[0], spread && spread[1]);
  refs.bar.setAttribute('aria-label', `${name} ${has(value) ? `${(+value).toFixed(1)}${range ? ` ${range.unit}` : ''}` : 'no reading'}${range ? `, ${goodText(range)}` : ''}`);
  const ch = spread ? `${range ? ' · ' : ''}channels ${spread[0].toFixed(1)} to ${spread[1].toFixed(1)}` : '';
  refs.scale.innerHTML = `<span>${range ? range.min : ''}</span><span class="good">${escapeHtml(goodText(range) + ch)}</span><span>${range ? range.max : ''}</span>`;
}

// A whole gauge as markup (the Channels dialog's summary).
export function gaugeMarkup(name, range, value, grade, spread) {
  const level = value === null || value === undefined ? 'unknown' : levelOfGrade(grade);
  const el = document.createElement('div');
  el.innerHTML = `<div class="gauge"><div class="g-name"><span data-f="ic"></span><span>${escapeHtml(name)}</span></div><div class="g-val"><span data-f="val"></span><small>${range ? escapeHtml(range.unit) : ''}</small></div><div class="g-bar" role="img" data-f="bar"></div><div class="g-scale" data-f="scale"></div></div>`;
  const g = el.firstElementChild;
  const refs = {};
  g.querySelectorAll('[data-f]').forEach((x) => { refs[x.dataset.f] = x; });
  paintGauge(g, refs, name, range, value, level === 'unknown' ? null : grade, spread);
  return g.outerHTML;
}

export const spreadOf = (channels, dir, key) => {
  const v = channels.filter((c) => c.direction === dir && has(c[key])).map((c) => c[key]);
  return v.length > 1 ? [Math.min(...v), Math.max(...v)] : null;
};

function interval(snap, device) {
  const s = snap.uncorrectables_interval_seconds || (device && device.poll_interval_seconds);
  if (!s) return 'since the last reading';
  return s >= 90 ? `in the last ${Math.round(s / 60)} min` : `in the last ${Math.round(s)} s`;
}

// The uncorrectable errors in words, with their context: how many channels and which frequencies, over what interval,
// and whether the probes lost packets meanwhile (the server's errors_* fields), graded by the server. The server's own
// note (counts, ratio) goes underneath as the detail. A big red number only when the server grades it critical.
export function errorsText(snap, device) {
  const recent = snap.uncorrectables_recent || 0;
  const grade = snap.grades && snap.grades.uncorrectables;
  if (!recent) return { level: 'good', text: 'No new uncorrectable errors.', detail: '' };
  const hit = (snap.channels || []).filter((c) => c.direction === 'downstream' && (c.uncorrectables_recent || 0) > 0);
  const count = snap.errors_affected_channels || hit.length;
  const span = snap.errors_span_mhz || (hit.length ? [Math.min(...hit.map((c) => c.frequency_mhz)), Math.max(...hit.map((c) => c.frequency_mhz))] : null);
  const mhz = span && span[0] ? ` (${Math.round(span[0]) === Math.round(span[1]) ? Math.round(span[0]) : `${Math.round(span[0])}–${Math.round(span[1])}`} MHz)` : '';
  const where = count ? ` on ${count} channel${count === 1 ? '' : 's'}${mhz}` : '';
  const loss = snap.errors_probe_loss === true ? ', with packet loss on the line meanwhile'
    : snap.errors_probe_loss === false ? ', no measurable loss' : '';
  const what = grade === 'critical' ? 'Heavy error burst' : snap.errors_recurring ? 'Recurring errors' : 'Brief error burst';
  return { level: grade ? levelOfGrade(grade) : 'unknown', text: `${what}${where} ${interval(snap, device)}${loss}.`, detail: snap.errors_note || '' };
}

function microBars(values, level) {
  if (!values.length) return '';
  const n = Math.max(values.length, 30);
  const w = n * 5;
  const h = 22;
  const mx = Math.max(10, ...values);
  const color = level === 'crit' ? 'var(--crit)' : level === 'warn' ? 'var(--warn-fill)' : 'var(--ink-3)';
  const off = n - values.length;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px;display:block" aria-hidden="true">${values.map((v, i) => {
    const hh = v ? Math.max(3, (v / mx) * (h - 2)) : 1.5;
    return `<rect x="${(i + off) * 5}" y="${(h - hh).toFixed(1)}" width="3.4" height="${hh.toFixed(1)}" rx="1" fill="${v ? color : 'var(--line-strong)'}"/>`;
  }).join('')}</svg>`;
}

export default {
  technology: 'docsis',
  defaultTitle: DEFAULT_TITLE, // the card's title when the device has no configured model
  detailAction: { action: 'openChannelsModal', icon: 'channels', label: 'Channels' },
  // the live summary has no channels: the panel fetches the full reading for them
  needsDetail: (snap) => !snap.channels && snap.channel_count !== 0,
  // Builds the body inside `host`; returns render(snapshot, context).
  mount(host, device) {
    const { root, refs } = cloneTemplate('tpl-line-docsis');
    host.replaceChildren(root);
    const gauge = (key) => {
      const el = refs[`g_${key}`];
      const r = {};
      el.querySelectorAll('[data-f]').forEach((x) => { r[x.dataset.f] = x; });
      return { el, r };
    };
    const g = { ds_power: gauge('ds_power'), ds_snr: gauge('ds_snr'), us_power: gauge('us_power'), uncorr: gauge('uncorr') };
    const seen = []; // [timestamp, new errors] per reading
    return (snap) => {
      const ch = snap.channels || [];
      paintGauge(g.ds_power.el, g.ds_power.r, 'Downstream power', rangeOf(device, 'ds_power'), snap.downstream_power_avg,
        gradeOf(snap, 'ds_power'), spreadOf(ch, 'downstream', 'power_dbmv'));
      paintGauge(g.ds_snr.el, g.ds_snr.r, 'Downstream SNR', rangeOf(device, 'ds_snr'), snap.downstream_snr_avg,
        gradeOf(snap, 'ds_snr'), spreadOf(ch, 'downstream', 'snr_db'));
      paintGauge(g.us_power.el, g.us_power.r, 'Upstream power', rangeOf(device, 'us_power'), snap.upstream_power_avg,
        gradeOf(snap, 'us_power'), spreadOf(ch, 'upstream', 'power_dbmv'));
      // errors: plain words, the count small unless the server says it is serious
      if (has(snap.timestamp) && (!seen.length || seen[seen.length - 1][0] !== snap.timestamp)) {
        seen.push([snap.timestamp, snap.uncorrectables_recent || 0]);
        if (seen.length > HISTORY) seen.shift();
      }
      const e = errorsText(snap, device);
      const u = g.uncorr;
      u.el.className = `gauge fec st-${e.level}${e.level === 'crit' ? ' serious' : ''}`;
      if (u.r.ic.dataset.level !== e.level) {
        u.r.ic.dataset.level = e.level;
        u.r.ic.innerHTML = icon(STATUS[e.level].icon, 'sm');
      }
      setText(u.r.val, (snap.uncorrectables_recent || 0).toLocaleString());
      setText(u.r.per, `new · ${(snap.uncorrectables_total || 0).toLocaleString()} since boot`);
      setText(u.r.note, e.text);
      u.r.note.title = e.detail;
      setText(u.r.detail, e.detail);
      // the bars only when a reading this page saw had errors: an empty strip of zeros said nothing
      const anyErrors = seen.some((s) => s[1] > 0);
      u.r.bars.hidden = !anyErrors;
      u.r.scale.hidden = !anyErrors;
      if (anyErrors) u.r.bars.innerHTML = microBars(seen.map((s) => s[1]), e.level);
      // channels: locked counts and one cell per downstream channel
      const down = ch.filter((c) => c.direction === 'downstream');
      const up = ch.filter((c) => c.direction === 'upstream');
      const errored = down.filter((c) => (c.uncorrectables_recent || 0) > 0);
      // a coloured cell is a channel the server grades outside its good range; say how many, so a lone amber cell
      // beside "no new errors" is not a riddle
      const offGrade = (c) => ['power', 'snr'].filter((k) => c.grades && (c.grades[k] === 'warn' || c.grades[k] === 'critical')).map((k) => (k === 'snr' ? 'SNR' : 'power'));
      const graded = down.filter((c) => offGrade(c).length).length;
      refs.chanTxt.innerHTML = ch.length
        ? `<b>${down.length}</b> down · <b>${up.length}</b> up locked · ${errored.length ? `<b>${errored.length}</b> with new errors` : 'no new errors'}`
          + (graded ? ` · <b>${graded}</b> outside the good range` : '')
        : (snap.channel_count ? `<b>${snap.channel_count}</b> channels` : '');
      refs.chanStrip.style.setProperty('--n', String(Math.max(1, down.length)));
      refs.chanStrip.setAttribute('aria-label', `${down.length} downstream channels, ${errored.length} with new errors`);
      refs.chanStrip.innerHTML = down.map((c) => {
        const bad = (c.uncorrectables_recent || 0) > 0;
        const worst = worstGrade(Object.values(c.grades || {}));
        const cls = worst === 'critical' ? 'c' : worst === 'warn' ? 'w' : '';
        const why = offGrade(c).map((w) => ` · ${w} outside its good range`).join('');
        return `<i class="${cls}" data-tip="Channel ${c.channel_id} · ${c.frequency_mhz} MHz${bad ? ` · +${c.uncorrectables_recent} errors` : ''}${why}" title="Channel ${c.channel_id}, ${c.frequency_mhz} MHz${bad ? `, +${c.uncorrectables_recent} errors` : ''}${why.replace(/ · /g, ', ')}"></i>`;
      }).join('');
    };
  }
};
