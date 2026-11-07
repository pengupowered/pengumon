// The DOCSIS adapter of the line card (model.js): downstream power, downstream SNR and upstream power as range gauges
// (good zone, warning zones, the spread across channels, the value), the new uncorrectable errors in plain words with
// their context, and the channel summary, all as the readings part of the card's view model (`reader`). Its detail view
// is the Channels dialog (channels.js). Every DOCSIS name and number lives here and in that view; the line card itself
// knows no technology.

import { escapeHtml, icon, setText, STATUS } from '../../js/util.js';
import { gradeOf, levelOfGrade, rangeOf, worstGrade } from '../../js/grades.js';
import { barMarkup, gaugeText, goodText, segments } from './gauge.js';
import './channels.js'; // the adapter's detail view (registers openChannelsModal)

export const DEFAULT_TITLE = 'Cable modem';
const has = (v) => v !== null && v !== undefined;
const HISTORY = 60; // readings kept for the error bars (since the page opened)

export { goodText };

function paintGauge(el, refs, name, range, value, grade, spread) {
  const level = has(value) ? levelOfGrade(grade) : 'unknown';
  el.className = `gauge st-${level}${has(value) ? '' : ' na'}`;
  if (refs.ic.dataset.level !== level) {
    refs.ic.dataset.level = level;
    refs.ic.innerHTML = icon(STATUS[level].icon, 'sm');
  }
  setText(refs.val, has(value) ? (+value).toFixed(1) : '–');
  const words = gaugeText(name, range, value, spread);
  refs.bar.innerHTML = barMarkup(segments(range, value, spread && spread[0], spread && spread[1]));
  refs.bar.setAttribute('aria-label', words.aria);
  refs.scale.innerHTML = `<span>${words.scale.min}</span><span class="good">${escapeHtml(words.scale.good)}</span><span>${words.scale.max}</span>`;
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

const GAUGES = [
  { key: 'ds_power', name: 'Downstream power', unit: 'dBmV', help: 'docsis.dspower', label: 'About downstream power', value: 'downstream_power_avg', dir: 'downstream', spread: 'power_dbmv' },
  { key: 'ds_snr', name: 'Downstream SNR', unit: 'dB', help: 'docsis.snr', label: 'About downstream snr', value: 'downstream_snr_avg', dir: 'downstream', spread: 'snr_db' },
  { key: 'us_power', name: 'Upstream power', unit: 'dBmV', help: 'docsis.uspower', label: 'About upstream power', value: 'upstream_power_avg', dir: 'upstream', spread: 'power_dbmv' }
];

// A channel the server grades outside its good range: which of power / SNR.
const offGrade = (c) => ['power', 'snr'].filter((k) => c.grades && (c.grades[k] === 'warn' || c.grades[k] === 'critical')).map((k) => (k === 'snr' ? 'SNR' : 'power'));

// A made-up modem and reading for the parity test of the card's views (model.js `fixture`): every part present.
const SAMPLE_DEVICE = {
  id: 'sample_modem', host: '192.0.2.1', poll_interval_seconds: 30, line_technology: 'docsis',
  ranges: {
    ds_power: { unit: 'dBmV', min: -15, max: 15, good_low: -8, good_high: 8, critical_low: -12, critical_high: 12 },
    ds_snr: { unit: 'dB', min: 20, max: 50, good_low: 33, critical_low: 30 },
    us_power: { unit: 'dBmV', min: 30, max: 60, good_low: 35, good_high: 51, critical_high: 54 }
  }
};
const SAMPLE_SNAP = {
  status: 'warning', timestamp: 1767225600, downstream_power_avg: 1.3, downstream_snr_avg: 31.8, upstream_power_avg: 44.5,
  grades: { ds_power: 'good', ds_snr: 'warn', us_power: 'good', uncorrectables: 'warn' },
  uncorrectables_recent: 12, uncorrectables_total: 4521, uncorrectables_interval_seconds: 1800, errors_affected_channels: 1,
  errors_span_mhz: [537, 537], errors_probe_loss: false, errors_note: '12 new uncorrectable errors on one channel.',
  channels: [
    { direction: 'downstream', channel_id: 1, frequency_mhz: 531, power_dbmv: 1.1, snr_db: 38.1, grades: { power: 'good', snr: 'good' }, uncorrectables_recent: 0 },
    { direction: 'downstream', channel_id: 2, frequency_mhz: 537, power_dbmv: 1.5, snr_db: 31.2, grades: { power: 'good', snr: 'warn' }, uncorrectables_recent: 12 },
    { direction: 'upstream', channel_id: 1, frequency_mhz: 30, power_dbmv: 44.5, grades: { power: 'good' }, uncorrectables_recent: 0 }
  ]
};

export default {
  technology: 'docsis',
  sample: { device: SAMPLE_DEVICE, snap: SAMPLE_SNAP },
  defaultTitle: DEFAULT_TITLE, // the card's title when the device has no configured model
  detailAction: { action: 'openChannelsModal', icon: 'channels', label: 'Channels' },
  // the live summary has no channels: the card fetches the full reading for them
  needsDetail: (snap) => !snap.channels && snap.channel_count !== 0,
  // Makes the card's readings part from a snapshot: `reader(device)` returns read(snapshot) -> {gauges: [...], errors,
  // channels} (docs/themes.md). It keeps the new errors of the readings this page saw, for the error bars.
  reader(device) {
    const seen = []; // [timestamp, new errors] per reading
    return (snap) => {
      const ch = snap.channels || [];
      const gauges = GAUGES.map((g) => {
        const range = rangeOf(device, g.key);
        const value = snap[g.value];
        const grade = gradeOf(snap, g.key);
        const level = has(value) ? levelOfGrade(grade) : 'unknown';
        const spread = spreadOf(ch, g.dir, g.spread);
        const words = gaugeText(g.name, range, value, spread);
        return {
          key: g.key, name: g.name, unit: g.unit, help: { key: g.help, label: g.label }, na: !has(value),
          status: { level, icon: STATUS[level].icon },
          value: has(value) ? (+value).toFixed(1) : '–',
          bar: segments(range, value, spread && spread[0], spread && spread[1]),
          aria: words.aria,
          scale: words.scale
        };
      });
      // errors: plain words, the count small unless the server says it is serious
      if (has(snap.timestamp) && (!seen.length || seen[seen.length - 1][0] !== snap.timestamp)) {
        seen.push([snap.timestamp, snap.uncorrectables_recent || 0]);
        if (seen.length > HISTORY) seen.shift();
      }
      const e = errorsText(snap, device);
      // the bars only when a reading this page saw had errors: an empty strip of zeros said nothing
      const counts = seen.map((s) => s[1]);
      const errors = {
        name: 'Uncorrectable errors', help: { key: 'docsis.uncorr', label: 'About uncorrectable errors' },
        status: { level: e.level, icon: STATUS[e.level].icon }, serious: e.level === 'crit',
        value: (snap.uncorrectables_recent || 0).toLocaleString(),
        per: `new · ${(snap.uncorrectables_total || 0).toLocaleString()} since boot`,
        note: e.text, detail: e.detail,
        bars: counts.some((n) => n > 0)
          ? { values: counts, level: e.level, aria: `New errors in each of the last ${counts.length} readings, up to ${Math.max(...counts).toLocaleString()}`, since: 'since this page opened', now: 'now' }
          : null
      };
      // channels: locked counts and one cell per downstream channel
      const down = ch.filter((c) => c.direction === 'downstream');
      const up = ch.filter((c) => c.direction === 'upstream');
      const errored = down.filter((c) => (c.uncorrectables_recent || 0) > 0);
      // a coloured cell is a channel the server grades outside its good range; say how many, so a lone amber cell
      // beside "no new errors" is not a riddle
      const graded = down.filter((c) => offGrade(c).length).length;
      const summary = ch.length
        ? [{ b: String(down.length) }, ' down · ', { b: String(up.length) }, ` up locked · ${errored.length ? '' : 'no new errors'}`]
        : (snap.channel_count ? [{ b: String(snap.channel_count) }, ' channels'] : []);
      if (ch.length && errored.length) summary.push({ b: String(errored.length) }, ' with new errors');
      if (ch.length && graded) summary.push(' · ', { b: String(graded) }, ' outside the good range');
      const channels = {
        parts: summary,
        strip: {
          n: Math.max(1, down.length),
          aria: `${down.length} downstream channels, ${errored.length} with new errors`,
          cells: down.map((c) => {
            const bad = (c.uncorrectables_recent || 0) > 0;
            const worst = worstGrade(Object.values(c.grades || {}));
            const why = offGrade(c).map((w) => ` · ${w} outside its good range`).join('');
            return {
              cls: worst === 'critical' ? 'c' : worst === 'warn' ? 'w' : '',
              tip: `Channel ${c.channel_id} · ${c.frequency_mhz} MHz${bad ? ` · +${c.uncorrectables_recent} errors` : ''}${why}`,
              title: `Channel ${c.channel_id}, ${c.frequency_mhz} MHz${bad ? `, +${c.uncorrectables_recent} errors` : ''}${why.replace(/ · /g, ', ')}`
            };
          })
        }
      };
      return { gauges, errors, channels };
    };
  }
};
