// Line card, the standard view: the technology's readings (range gauges, the new errors in plain words, the channel
// summary) drawn from model.js's view model, or the sentence for a technology with no adapter. Each reading is a
// standard part renderer, (box) -> {update(part), unmount()}: a theme's view draws some itself and reuses the rest
// (ctx.std.parts), and a gauge a driver offers later is just one more entry of vm.readings.gauges. The parts know no
// technology. docs/themes.md, "Drawing a card".

import { kit } from '../../js/cards.js';
import { errorPart } from '../../js/device-view.js';
import { barMarkup } from './gauge.js';

const { el, escapeHtml, html, icon, mark, text, STATUS } = kit;

const info = (h) => `<button type="button" class="info" data-action="toggleHelp" data-help="${escapeHtml(h.key)}" aria-label="${escapeHtml(h.label)}">${icon('info')}</button>`;

// The state classes of a gauge box: st-<level>, and the extra ones (na, serious), the box's own classes kept.
function setState(box, level, extra) {
  const key = `${level}|${extra.join()}`;
  if (box.dataset.state === key) return;
  box.dataset.state = key;
  [...box.classList].filter((c) => c.startsWith('st-') || c === 'na' || c === 'serious').forEach((c) => box.classList.remove(c));
  box.classList.add(`st-${level}`, ...extra);
}

// The name line of a gauge: the status icon, the name and its help button (written once; the name never changes).
function nameLine(box, g) {
  const line = box.querySelector('.g-name');
  if (line.dataset.ready) return line.querySelector('[data-f=ic]');
  line.dataset.ready = '1';
  line.innerHTML = `<span data-f="ic"></span><span data-f="name"></span>${g.help ? info(g.help) : ''}`;
  mark(line.querySelector('[data-f=name]'), 'name').textContent = g.name;
  return line.querySelector('[data-f=ic]');
}

// The status of a gauge's name line, marked as the status: its icon and its word (.st-word; also data-word, for a theme
// that draws the status its own way, docs/themes.md "Restyling a card").
function status(ic, st) {
  const word = (STATUS[st.level] || STATUS.unknown).word;
  ic.dataset.word = word;
  kit.status(mark(ic, 'status'), st.level, word, { icon: st.icon, iconClass: 'sm', wordClass: 'st-word' });
}

// One range gauge (downstream power, SNR, upstream power) into `box`: status icon, name, help, the figure with its unit,
// the bar with the good zone, the value and the spread, and the scale under it.
function gauge(box) {
  box.classList.add('gauge');
  box.innerHTML = '<div class="g-name"></div><div class="g-val"><span data-f="val">–</span><small data-f="unit"></small></div><div class="g-bar" role="img" data-f="bar"></div><div class="g-scale" data-f="scale"></div>';
  const val = mark(box.querySelector('[data-f=val]'), 'value');
  const unit = mark(box.querySelector('[data-f=unit]'), 'unit');
  const bar = mark(box.querySelector('[data-f=bar]'), 'bar');
  const scale = mark(box.querySelector('[data-f=scale]'), 'scale');
  return {
    update(g) {
      box.dataset.item = g.key;
      status(nameLine(box, g), g.status);
      setState(box, g.status.level, g.na ? ['na'] : []);
      text(val, g.value);
      text(unit, g.unit);
      html(bar, barMarkup(g.bar));
      bar.setAttribute('aria-label', g.aria);
      html(scale, `<span>${escapeHtml(g.scale.min)}</span><span class="good">${escapeHtml(g.scale.good)}</span><span>${escapeHtml(g.scale.max)}</span>`);
    },
    unmount() {}
  };
}

// The new errors per reading as small bars (SVG, token colours), the page's own history.
function microBars(values, level) {
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

// The uncorrectable errors into `box`: the count (big only when the server says it is serious), what happened in plain
// words, the server's note, and the bars of the readings this page saw (only once one had errors).
function errors(box) {
  box.classList.add('gauge', 'fec');
  box.innerHTML = `<div class="g-name"></div><div class="g-val"><span data-f="val">0</span><small data-f="per">new</small></div>
    <p class="fec-note" data-f="note"></p><p class="fec-detail" data-f="detail"></p><div class="g-bar-free" role="img" data-f="bars"></div>
    <div class="g-scale" data-f="scale"><span data-f="since"></span><span data-f="now"></span></div>`;
  const val = mark(box.querySelector('[data-f=val]'), 'count');
  const per = mark(box.querySelector('[data-f=per]'), 'per');
  const note = mark(box.querySelector('[data-f=note]'), 'note');
  const detail = mark(box.querySelector('[data-f=detail]'), 'detail');
  const bars = mark(box.querySelector('[data-f=bars]'), 'bars');
  const scale = mark(box.querySelector('[data-f=scale]'), 'barsScale');
  return {
    update(e) {
      box.dataset.item = 'errors';
      status(nameLine(box, e), e.status);
      setState(box, e.status.level, e.serious ? ['serious'] : []);
      text(val, e.value);
      text(per, e.per);
      text(note, e.note);
      note.title = e.detail;
      text(detail, e.detail);
      bars.hidden = !e.bars;
      scale.hidden = !e.bars;
      if (e.bars) {
        html(bars, microBars(e.bars.values, e.bars.level));
        bars.setAttribute('aria-label', e.bars.aria);
        text(box.querySelector('[data-f=since]'), e.bars.since);
        text(box.querySelector('[data-f=now]'), e.bars.now);
      }
    },
    unmount() {}
  };
}

// The channel summary into `box`: locked counts in words and one cell per downstream channel (amber and red where the
// server grades the channel outside its range).
function channels(box) {
  box.classList.add('chan-sum');
  box.innerHTML = '<div class="chan-txt" data-f="chanTxt"></div><div class="strip slim chan-strip" role="img" data-f="chanStrip"></div>';
  const txt = mark(box.querySelector('[data-f=chanTxt]'), 'summary');
  const strip = mark(box.querySelector('[data-f=chanStrip]'), 'strip');
  return {
    update(c) {
      box.dataset.item = 'channels';
      html(txt, c.parts.map((p) => (typeof p === 'string' ? escapeHtml(p) : `<b>${escapeHtml(p.b)}</b>`)).join(''));
      strip.style.setProperty('--n', String(c.strip.n));
      strip.setAttribute('aria-label', c.strip.aria);
      html(strip, c.strip.cells.map((x) => `<i class="${x.cls}" data-tip="${escapeHtml(x.tip)}" title="${escapeHtml(x.title)}"></i>`).join(''));
    },
    unmount() {}
  };
}

// All of a reading's parts into `box`, in order: the gauges (one per entry of vm.readings.gauges), the errors, the
// channel summary. A part the technology does not offer is null and is not drawn.
function readings(box) {
  box.classList.add('line-docsis');
  const gauges = new Map(); // key -> { box, part }
  const errBox = box.appendChild(el('div'));
  errBox.dataset.f = 'g_uncorr';
  const chanBox = box.appendChild(el('div'));
  const err = errors(errBox);
  const chan = channels(chanBox);
  return {
    update(r) {
      const keep = new Set(r.gauges.map((g) => g.key));
      gauges.forEach((x, k) => { if (!keep.has(k)) { x.box.remove(); gauges.delete(k); } });
      r.gauges.forEach((g) => {
        let x = gauges.get(g.key);
        if (!x) {
          const b = el('div');
          box.insertBefore(b, errBox);
          x = { box: b, part: gauge(b) };
          b.dataset.f = `g_${g.key}`;
          gauges.set(g.key, x);
        }
        x.part.update(g);
      });
      errBox.hidden = !r.errors;
      chanBox.hidden = !r.channels;
      if (r.errors) err.update(r.errors);
      if (r.channels) chan.update(r.channels);
    },
    unmount() {}
  };
}

// The sentence for a technology with no adapter.
function note(box) {
  return { update(t) { box.replaceChildren(mark(el('p', 'cap-note', t), 'note')); }, unmount() {} };
}

export const parts = { gauge, errors, channels, readings, note, error: errorPart };

export default {
  parts,
  mount(root) {
    const body = root.querySelector('[data-f=body]');
    const error = errorPart(root.querySelector('[data-f=error]'));
    let drawn = null; // the part standing in the body (the skeleton until there is one)
    return {
      update(vm, prev) {
        if (!prev || prev.error !== vm.error) error.update(vm.error);
        body.classList.toggle('dim', vm.dim);
        if (vm.note && !drawn) {
          drawn = note(body);
          drawn.update(vm.note);
        } else if (vm.readings) {
          if (!drawn) {
            const box = el('div');
            body.replaceChildren(box);
            drawn = readings(box);
          }
          if (!prev || prev.readings !== vm.readings) drawn.update(vm.readings);
        }
      },
      unmount() { if (drawn) drawn.unmount(); }
    };
  }
};
