// Router card, the standard view: one block per part the device offers (WAN traffic, system, clients, mesh nodes), drawn
// from model.js's view model. Each block is a standard part renderer, (box) -> {update(part), unmount()}, so a theme's
// view can draw some parts itself and reuse the rest (ctx.std.parts), and a driver that offers a part later needs no
// theme change: a part of the view model that is null is not offered. docs/themes.md, "Drawing a card".

import { kit } from '../../js/cards.js';
import { errorPart } from '../../js/device-view.js';

const { el, escapeHtml, html, icon, list, mark, text } = kit;
const has = (v) => v !== null && v !== undefined;

const COLOURS = { warn: 'var(--warn)', crit: 'var(--crit)' };
const info = (key, label) => `<button type="button" class="info" data-action="toggleHelp" data-help="${key}" aria-label="${label}">${icon('info')}</button>`;

// A block's title is h3.rb-t, its name in span.rb-n (a theme can style the name alone).

// One meter (Download, Upload, CPU, Memory) into `box` (a div.meter): the label, the figure with its unit, the bar.
function meter(box) {
  box.classList.add('meter');
  box.innerHTML = '<span class="lbl"></span><span class="val"><span data-f="v">–</span><small data-f="of"></small></span><div class="track" role="img"><div class="fill"></div></div>';
  const lbl = box.querySelector('.lbl');
  const v = box.querySelector('[data-f=v]');
  const unit = box.querySelector('[data-f=of]');
  const track = box.querySelector('.track');
  return {
    update(m) {
      html(lbl, `${m.icon ? icon(m.icon, 'sm') : ''}${escapeHtml(m.label)}${m.help ? ` ${info(m.help.key, m.help.label)}` : ''}`);
      mark(v, m.key);
      mark(unit, `${m.key}Unit`);
      mark(track, `${m.key}Load`);
      text(v, m.value);
      text(unit, ` ${m.unit}`);
      box.style.setProperty('--mc', COLOURS[m.level] || 'var(--accent)');
      box.style.setProperty('--v', `${m.pct ?? 0}%`); // the fill's share (components.css), for a theme to draw it its way
      track.setAttribute('aria-label', m.aria);
    },
    unmount() {}
  };
}

// The last hour's download as an area chart (SVG, token colours), from the chart data of the view model.
function chartSvg(c) {
  const W = 320;
  const H = 70;
  const L = 60;
  const max = c.top * 1.08;
  const x = (i) => L + (i / Math.max(1, c.vals.length - 1)) * (W - L - 4);
  const y = (v) => 6 + (1 - v / max) * (H - 18);
  let d = '';
  let pen = false;
  c.vals.forEach((v, i) => {
    if (!has(v)) { pen = false; return; }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
  });
  const first = c.vals.findIndex(has);
  let last = c.vals.length - 1;
  while (!has(c.vals[last])) last--;
  const capLine = c.cap ? `<line x1="${L}" x2="${W - 4}" y1="${y(c.cap).toFixed(1)}" y2="${y(c.cap).toFixed(1)}" stroke="var(--ink-3)" stroke-dasharray="4 4"/><text x="${L + 4}" y="${(y(c.cap) - 4).toFixed(1)}" style="fill:var(--ink-2)">${escapeHtml(c.capText)}</text>` : '';
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${escapeHtml(c.aria)}">
    <line class="base" x1="${L}" x2="${W - 4}" y1="${y(0)}" y2="${y(0)}"/><text x="${L - 6}" y="${y(0) + 3}" text-anchor="end">0</text><text x="${L - 6}" y="${y(c.top) + 3}" text-anchor="end">${escapeHtml(c.topText)}</text>
    ${capLine}<path d="${d}L${x(last).toFixed(1)} ${y(0)}L${x(first).toFixed(1)} ${y(0)}Z" fill="var(--s1)" opacity=".18"/><path d="${d}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(last).toFixed(1)}" cy="${y(c.vals[last]).toFixed(1)}" r="3.5" fill="var(--s1)" stroke="var(--surface-1)" stroke-width="2"/>
    <text x="${L}" y="${H - 1}">60 min ago</text><text x="${W - 4}" y="${H - 1}" text-anchor="end">now</text></svg>
    <p class="fine" data-v="chart">${escapeHtml(c.caption)}</p></div>`;
}

// The WAN chart into `box`: the area chart and its caption, or the sentence saying there are no readings yet.
function chart(box) {
  return {
    update(c) {
      if (!c) html(box, '');
      else html(box, c.empty ? `<p class="fine" data-v="chart">${escapeHtml(c.empty)}</p>` : chartSvg(c));
    },
    unmount() {}
  };
}

// WAN traffic: download and upload against the line capacity, and the last hour.
function wan(box) {
  box.innerHTML = `<h3 class="rb-t">${icon('activity', 'sm')}<span class="rb-n">WAN traffic</span> <span class="muted" data-f="capNote"></span><button type="button" class="info" data-action="toggleHelp" data-help="router.wan" aria-label="About WAN traffic">${icon('info')}</button></h3>
    <div data-f="rx"></div><div data-f="tx"></div><div class="wan-chart" data-f="wanChart"></div>`;
  const note = mark(box.querySelector('[data-f=capNote]'), 'capNote');
  const rx = meter(box.querySelector('[data-f=rx]'));
  const tx = meter(box.querySelector('[data-f=tx]'));
  const chartPart = chart(box.querySelector('[data-f=wanChart]'));
  return {
    update(w, prev) {
      text(note, w.note);
      rx.update(w.rx);
      tx.update(w.tx);
      if (!prev || prev.chart !== w.chart) chartPart.update(w.chart);
    },
    unmount() {}
  };
}

// System: CPU and memory.
function system(box) {
  box.innerHTML = `<h3 class="rb-t">${icon('cpu', 'sm')}<span class="rb-n">System</span></h3><div data-f="cpu"></div><div data-f="mem"></div>`;
  const cpu = meter(box.querySelector('[data-f=cpu]'));
  const mem = meter(box.querySelector('[data-f=mem]'));
  return {
    update(s) {
      cpu.update(s.cpu);
      mem.update(s.mem);
    },
    unmount() {}
  };
}

// Clients: how many are online (it opens the Local network dialog) and the split by band.
function clients(box) {
  box.innerHTML = `<h3 class="rb-t">${icon('users', 'sm')}<span class="rb-n">Clients</span><button type="button" class="info" data-action="toggleHelp" data-help="router.clients" aria-label="About active clients">${icon('info')}</button></h3>
    <button type="button" class="big-count stat-open" data-f="count" data-action="openNetworkModal"><b class="num" data-f="countVal">–</b><span>online</span></button>
    <div class="stackbar" role="img" data-f="bands"></div><div class="legend" data-f="bandLegend"></div>`;
  const count = mark(box.querySelector('[data-f=countVal]'), 'online');
  const bands = mark(box.querySelector('[data-f=bands]'), 'bands');
  const legend = mark(box.querySelector('[data-f=bandLegend]'), 'legend');
  return {
    update(c) {
      text(count, c.count);
      html(bands, c.bands.map((b) => `<i style="flex:${b.n};--c:var(${b.colour})" title="${escapeHtml(b.title)}"></i>`).join(''));
      bands.setAttribute('aria-label', c.aria);
      html(legend, c.bands.map((b) => `<span><i class="key" style="--c:var(${b.colour})"></i>${escapeHtml(b.name)} <b class="num">${b.n}</b></span>`).join(''));
    },
    unmount() {}
  };
}

// One mesh node's row: its name and backhaul, offline in words, and its client count as a button that opens the clients.
function node(n) {
  const li = el('li');
  if (n.status) li.className = 'st-warn';
  const c = n.clients;
  li.innerHTML = `<span class="n-ic">${icon('mesh')}</span><span class="n-id"><b></b><span></span></span><span class="n-st"></span>
    <button type="button" class="n-c" data-action="${escapeHtml(c.action)}" data-arg="${escapeHtml(c.arg)}"><b class="num"></b><i></i></button>`;
  mark(li.querySelector('.n-id b'), 'name').textContent = n.label;
  mark(li.querySelector('.n-id span'), 'sub').textContent = n.sub;
  if (n.status) kit.status(mark(li.querySelector('.n-st'), 'status'), n.status.level, n.status.word, { icon: n.status.icon });
  const b = mark(li.querySelector('.n-c'), 'clients');
  b.setAttribute('aria-label', c.label);
  b.title = c.title;
  b.querySelector('b').textContent = c.count;
  b.querySelector('i').style.width = `${c.pct}%`;
  return li;
}

// Mesh nodes: one row each, with the number of clients on it.
function mesh(box) {
  box.innerHTML = `<h3 class="rb-t">${icon('mesh', 'sm')}<span class="rb-n">Mesh nodes</span> <span class="muted" data-f="meshCount"></span></h3><ul class="nodes" data-f="nodes"></ul>`;
  const count = mark(box.querySelector('[data-f=meshCount]'), 'count');
  const nodes = box.querySelector('[data-f=nodes]');
  return {
    update(m) {
      text(count, m.count);
      list(nodes, m.nodes, { key: (n) => n.id, render: node });
    },
    unmount() {}
  };
}

// The sentence saying why a part is missing (a driver without a client list).
function notice(box) {
  box.innerHTML = `${icon('info', 'sm')}<span></span>`;
  const span = mark(box.querySelector('span'), 'notice');
  return { update(t) { box.hidden = !t; text(span, t); }, unmount() {} };
}

export const parts = { meter, chart, wan, system, clients, mesh, node, notice, error: errorPart };

const BLOCKS = [['wan', wan], ['sys', system, 'system'], ['clients', clients], ['mesh', mesh]];

export default {
  parts,
  mount(root) {
    const grid = root.querySelector('[data-f=grid]');
    const error = errorPart(root.querySelector('[data-f=error]'));
    const note = notice(root.querySelector('[data-f=capNoteMissing]'));
    const blocks = BLOCKS.map(([ref, part, key = ref]) => {
      const box = root.querySelector(`[data-f=${ref}]`);
      return { box, key, part: part(box) };
    });
    return {
      update(vm, prev) {
        if (!prev || prev.error !== vm.error) error.update(vm.error);
        root.querySelector('[data-f=body]').classList.toggle('dim', vm.dim);
        if (!prev || prev.notice !== vm.notice) note.update(vm.notice);
        blocks.forEach((b) => {
          const p = vm[b.key];
          b.box.hidden = !p;
          if (p && (!prev || prev[b.key] !== p)) b.part.update(p, prev && prev[b.key]);
        });
        grid.className = `rgrid n${blocks.filter((b) => vm[b.key]).length}`;
      },
      unmount() { blocks.forEach((b) => b.part.unmount()); }
    };
  }
};
