// Path health, the standard view: the verdict (badge, title, sentence, Inspect), the notice over the chain, the chain
// (this host, then a node per stage with the links between), the other targets under it and the legend, drawn from
// model.js's view model. The chain's elements are made once per set of nodes and then patched in place: the message
// comes every second and the figures change rarely. docs/themes.md, "Drawing a card"; a theme's own view of this card
// gets these parts as ctx.std.parts, with the parts for a layout of its own at the end of this file (verdictLine,
// hopFields, extraChips, blockTrace).

import { kit } from '../../js/cards.js';
import { setBadge, setText } from '../../js/util.js';
import { sparkSvg } from '../../js/sparkline.js';

const { escapeHtml, icon } = kit;

// ---------- the verdict ----------
function verdict(box) {
  const badge = box.querySelector('.badge');
  badge.dataset.v = 'verdict';
  box.querySelector('h3').dataset.v = 'title';
  box.querySelector('p').dataset.v = 'text';
  return {
    update(v) {
      setBadge(badge, v.level, v.word);
      badge.dataset.st = v.level;
      setText(box.querySelector('h3'), v.title);
      setText(box.querySelector('p'), v.text);
      // Inspect: shown while an incident is open
      let button = box.querySelector('.verdict-act');
      if (!v.inspect) { if (button) button.remove(); return; }
      if (!button) button = box.appendChild(kit.action(v.inspect, { className: 'btn outline sm verdict-act' }));
      button.dataset.arg = v.inspect.arg;
    }
  };
}

// ---------- the notice over the chain (first run, route changed) ----------
function noticeHtml(b) {
  const words = b.progress ? `<span data-f="progress" data-v="noticeText">${escapeHtml(b.text)}</span>` : `<span data-v="noticeText">${escapeHtml(b.text)}</span>`;
  const buttons = b.actions.map((a) => `<button type="button" class="btn sm${a.primary ? ' primary' : ''}" data-action="${escapeHtml(a.action)}"${a.needsEdit ? ' data-needs-edit' : ''}`
    + `${a.arg !== undefined ? ` data-arg="${escapeHtml(a.arg)}"` : ''}>${escapeHtml(a.label)}</button>`).join('');
  return `<div class="result st-${b.level}" data-st="${b.level}">${icon(b.icon)}<span><b data-v="notice">${escapeHtml(b.title)}</b> ${words}</span>${buttons}</div>`;
}

function notice(box) {
  return {
    update(b) {
      box.hidden = !b;
      kit.html(box, b ? noticeHtml(b) : '');
    }
  };
}

// ---------- the chain ----------
// The origin of the chain: a small marker, not a hop (no status, no value). Hop statuses are on the nodes and links.
function hostNode(count) {
  return `<li class="origin" aria-label="This host: probing ${count} targets every second"><span class="origin-ic">${icon('host')}</span>
    <b class="origin-t">This host</b><span class="origin-m"><span data-f="hostCount" data-v="hostCount">${count}</span> targets</span><span class="origin-m">every 1 s</span></li>`;
}

function nodeShell(n) {
  const key = escapeHtml(n.key);
  return `<li class="link" data-link="${key}" data-item="${key}" aria-hidden="true"><span class="wirebox"><svg class="wire" viewBox="0 0 40 10" preserveAspectRatio="none"><line x1="0" y1="5" x2="40" y2="5"/></svg></span><span class="cut" hidden>${icon('close', 'sm')}</span><span class="delta" data-v="delta"></span></li>
    <li class="node-wrap" data-item="${key}"><button type="button" class="node" data-node="${key}" data-action="openTargetHistory" data-range="live" data-arg="${escapeHtml(n.target)}"${n.group ? ` data-group="${escapeHtml(n.group)}"` : ''}>
      <span class="node-top"><span class="node-ic">${icon(n.icon)}</span><span class="node-id"><span class="node-kind" data-v="kind"></span></span><span class="node-st" data-v="status"></span></span><span class="node-name" data-v="name"></span>
      <div class="node-val"></div><div class="node-meta"></div><span class="node-spark"></span></button></li>`;
}

function skeleton() {
  const node = '<li class="link" aria-hidden="true"></li><li class="node-wrap"><div class="node st-unknown"><span class="node-top"><span class="node-ic"></span><span class="skel" style="width:80px;height:12px"></span></span><div class="node-val"><span class="skel" style="width:58px;height:26px"></span></div><div class="node-meta"><span class="skel" style="width:90px;height:10px"></span></div><div class="skel" style="height:26px"></div></div></li>';
  return `<li class="origin"><span class="origin-ic">${icon('host')}</span><b class="origin-t">This host</b><span class="origin-m">connecting…</span></li>${node.repeat(3)}`;
}

// One node and the link before it, patched: the three boxes of figures are rewritten only when their HTML differs
// (rewriting identical HTML still rebuilds the nodes and, for the sparkline, its SVG). The status is its icon and its word (.st-word, also its
// title), and a figure's name is span.lbl, so a theme can show them its way (docs/themes.md, "Restyling a card").
function paintNode(el, link, n, painted) {
  // what a click opens follows the node (its worst target, or the members of a group can change under the same key)
  if (el.dataset.arg !== n.target) el.dataset.arg = n.target;
  if ((el.dataset.group || '') !== n.group) {
    if (n.group) el.dataset.group = n.group;
    else delete el.dataset.group;
  }
  el.className = `node st-${n.level}${n.alert ? ' alert' : ''}`;
  el.setAttribute('aria-label', n.aria);
  setText(el.querySelector('.node-kind'), n.label);
  // the configured name is long ("Hop 3: <provider> ..."): one line under the short label, whole in the tooltip
  const name = el.querySelector('.node-name');
  setText(name, n.name);
  name.title = n.nameTitle;
  const st = el.querySelector('.node-st');
  kit.status(st, n.level, n.word, { icon: n.statusIcon, wordClass: 'st-word' });
  if (st.title !== n.word) st.title = n.word;
  const val = n.value.word ? `<b class="node-word" data-v="value">${escapeHtml(n.value.word)}</b>` : `<b class="num" data-v="value">${escapeHtml(n.value.num)}</b><span>${escapeHtml(n.value.unit)}</span>`;
  const meta = n.quiet ? `<span title="${escapeHtml(n.quiet.why)}" data-v="quiet">${escapeHtml(n.quiet.text)}</span>`
    : `<span class="${n.loss.bad ? 'bad' : ''}"><span class="lbl">loss</span> <b class="num" data-v="loss">${escapeHtml(n.loss.text)}</b></span>${n.jitter !== null ? `<span><span class="lbl">jitter</span> <b class="num" data-v="jitter">${escapeHtml(n.jitter)}</b></span>` : ''}`;
  const spark = sparkSvg(n.spark, n.level, { id: n.key }); // the same samples give the same markup (its gradient id is the node's)
  const last = painted.get(el) || {};
  if (last.val !== val) el.querySelector('.node-val').innerHTML = val;
  if (last.meta !== meta) el.querySelector('.node-meta').innerHTML = meta;
  if (last.spark !== spark) el.querySelector('.node-spark').innerHTML = spark;
  painted.set(el, { val, meta, spark });
  link.className = `link st-${n.link.level}${n.link.broken ? ' broken' : ''}`;
  link.querySelector('.cut').hidden = !n.link.broken;
  const delta = link.querySelector('.delta');
  delta.title = n.link.title;
  setText(delta, n.link.text);
}

// How many nodes fit side by side before a label like "ISP hop 3" or a value like "23.2 ms" no longer does (the
// chain's dense styles: links of 34 px, 24 px in a medium hero); a vertical chain (narrow hero) takes any number.
const MIN_NODE = 112;

// The chain, from the path's nodes; the ISP hops fold into one node when there is no room for a node each.
function chain(list) {
  let key = null; // the nodes drawn, null until the first message replaces the skeleton
  let box = null; // { column, width } of the chain, null until measured
  let path = null;
  const painted = new WeakMap(); // node element -> the HTML last written to its value, meta and sparkline boxes
  // measuring forces a layout, so it is done when the chain's size changes (the ResizeObserver), not on every message
  const measure = () => { box = { column: getComputedStyle(list).flexDirection === 'column', width: list.clientWidth }; };
  const fits = () => (box.column ? Infinity : Math.floor((box.width - 74) / (MIN_NODE + (box.width < 1060 ? 24 : 34))));
  list.innerHTML = skeleton();
  function paint() {
    if (!box) measure();
    let nodes = path.nodes;
    if (nodes.length > fits()) nodes = path.folded();
    const now = nodes.map((n) => n.key).join('|');
    if (now !== key) {
      key = now;
      list.innerHTML = hostNode(path.hostCount) + (nodes.length ? nodes.map(nodeShell).join('') : `<li class="chain-none" data-v="none">${escapeHtml(path.none)}</li>`);
    }
    // more than five nodes, or four in a medium hero: narrower links and padding, no node icons in a medium hero
    list.dataset.dense = nodes.length + 1 > 5 || (nodes.length >= 4 && box.width < 1060) ? '1' : '0';
    list.classList.add('is-live');
    setText(list.querySelector('[data-f=hostCount]'), String(path.hostCount));
    nodes.forEach((n) => {
      const el = list.querySelector(`[data-node="${CSS.escape(n.key)}"]`);
      const link = list.querySelector(`[data-link="${CSS.escape(n.key)}"]`);
      if (el && link) paintNode(el, link, n, painted);
    });
  }
  // a wider or narrower hero may fit a node per ISP hop, or no longer
  const watch = new ResizeObserver(() => {
    measure();
    if (path) paint();
  });
  watch.observe(list);
  return {
    update(p) { path = p; paint(); },
    unmount() { watch.disconnect(); }
  };
}

// The targets that are no stage of the path: one chip each under the chain.
function extras(box) {
  const list = box.querySelector('.pe-list');
  return {
    update(items) {
      box.hidden = !items.length;
      const html = items.map((t) => `<button type="button" class="pe st-${t.level}" data-item="${escapeHtml(t.id)}" data-st="${t.level}" data-action="openTargetHistory" data-arg="${escapeHtml(t.id)}" title="${escapeHtml(t.title)}">`
        + `${icon(t.icon, 'sm')}<span class="pe-n" data-v="name">${escapeHtml(t.name)}</span><b class="num" data-v="ms">${escapeHtml(t.ms)}</b></button>`).join('');
      if (list.dataset.html !== html) { list.dataset.html = html; list.innerHTML = html; }
    }
  };
}

// The legend under the chain: Busy (expected) joins it where the line's load is known.
function legend(foot) {
  return {
    update(busy) {
      if (!busy || !foot || foot.querySelector('[data-legend=busy]')) return;
      const item = document.createElement('span');
      item.dataset.legend = 'busy';
      item.innerHTML = `${icon('busy', 'sm ink-busy')}Busy (expected)`;
      foot.children[2].after(item);
    }
  };
}

// ---------- parts for a theme's own layout of the card ----------
// A theme view that lays the card out its own way (Amber CRT, Digital rain, Synthwave) builds it from these, so only its
// layout is its own. Their elements are named with the theme's class prefix (`ac` gives .ac-verdict, .ac-vtag, ...).

// The verdict: <div.<p>-verdict st-<level>> holding the status tag (.<p>-vtag, icon + word), the title (h3.<p>-vt) and
// the sentence (p.<p>-vp) in .<p>-vwords, then Inspect (.<p>-inspect) while an incident is open. Appended to `parent`.
function verdictLine(parent, prefix) {
  const box = parent.appendChild(kit.el('div', `${prefix}-verdict`));
  const tag = kit.mark(box.appendChild(kit.el('span', `${prefix}-vtag`)), 'verdict', 'unknown');
  const words = box.appendChild(kit.el('div', `${prefix}-vwords`));
  const title = kit.mark(words.appendChild(kit.el('h3', `${prefix}-vt`)), 'title');
  const text = kit.mark(words.appendChild(kit.el('p', `${prefix}-vp`)), 'text');
  let inspect = null;
  return {
    update(v) {
      box.className = `${prefix}-verdict st-${v.level}`;
      kit.status(tag, v.level, v.word);
      kit.text(title, v.title);
      kit.text(text, v.text);
      if (v.inspect && !inspect) inspect = box.appendChild(kit.action(v.inspect, { className: `btn outline sm ${prefix}-inspect` }));
      if (!inspect) return;
      inspect.hidden = !v.inspect;
      if (v.inspect) inspect.dataset.arg = v.inspect.arg;
    }
  };
}

// One hop of the chain, as fields for the view to place: make() -> { hop, st, kind, name, val, meta, delta }, where
// `hop` is the button that opens the hop's history (.<p>-hop, or `hopClass`) and the others are its fields, made but not
// placed: the status (.<p>-st, icon + word), the short label (.<p>-kind), the configured name (.<p>-name), the reading
// (.<p>-val: b.<p>-num and " ms"), loss and jitter or why the hop is quiet (.<p>-meta, figures b.<p>-n) and what its
// segment adds (.<p>-delta, .broken when the hop does not answer). patch(fields, node) writes a node of the view model
// into them, changing only what differs.
function hopFields(prefix, hopClass = `${prefix}-hop`) {
  const { el, mark, text } = kit;
  const figure = (word, key, className) => {
    const box = el('span', `${prefix}-${className}`);
    const n = mark(el('b', `${prefix}-n`), key);
    box.append(`${word} `, n);
    return [box, n];
  };
  return {
    make() {
      const hop = el('button', hopClass);
      hop.type = 'button';
      hop.dataset.action = 'openTargetHistory';
      hop.dataset.range = 'live';
      const f = {
        hop,
        st: mark(el('span', `${prefix}-st`), 'status', 'unknown'),
        kind: mark(el('span', `${prefix}-kind`), 'kind'),
        name: mark(el('span', `${prefix}-name`), 'name'),
        val: el('span', `${prefix}-val`),
        value: mark(el('b', `${prefix}-num`), 'value'),
        unit: el('span', `${prefix}-unit`, ' ms'),
        meta: el('span', `${prefix}-meta`),
        quiet: mark(el('span', `${prefix}-quiet`), 'quiet'),
        delta: mark(el('span', `${prefix}-delta`), 'delta')
      };
      f.val.append(f.value, f.unit);
      [f.lossBox, f.loss] = figure('loss', 'loss', 'loss');
      [f.jitBox, f.jit] = figure('jitter', 'jitter', 'jit');
      f.meta.append(f.lossBox, f.jitBox, f.quiet);
      return f;
    },
    patch(f, n) {
      const cls = `${hopClass} st-${n.level}${n.alert ? ' alert' : ''}`;
      if (f.hop.className !== cls) f.hop.className = cls;
      if (f.hop.dataset.arg !== n.target) f.hop.dataset.arg = n.target;
      if (n.group) f.hop.dataset.group = n.group; else delete f.hop.dataset.group;
      if (f.hop.getAttribute('aria-label') !== n.aria) f.hop.setAttribute('aria-label', n.aria);
      kit.status(f.st, n.level, n.word, { icon: n.statusIcon });
      text(f.kind, n.label);
      text(f.name, n.name);
      f.name.title = n.nameTitle;
      text(f.value, n.value.word || n.value.num);
      f.unit.hidden = !!n.value.word;
      f.lossBox.hidden = f.jitBox.hidden = !!n.quiet;
      f.quiet.hidden = !n.quiet;
      if (n.quiet) {
        text(f.quiet, n.quiet.text);
        f.quiet.title = n.quiet.why;
      } else {
        text(f.loss, n.loss.text);
        f.lossBox.classList.toggle('bad', !!n.loss.bad);
        f.jitBox.hidden = n.jitter === null;
        if (n.jitter !== null) text(f.jit, n.jitter);
      }
      text(f.delta, n.link.text);
      f.delta.title = n.link.title;
      f.delta.classList.toggle('broken', !!n.link.broken);
    }
  };
}

// The targets that are no stage of the path, as chips (.<p>-pe: icon, name, figure b.<p>-n) under a heading, in
// div.<p>-extra appended to `parent` (hidden while there are none).
function extraChips(parent, prefix, heading) {
  const box = parent.appendChild(kit.el('div', `${prefix}-extra`));
  box.hidden = true;
  box.append(kit.el('h4', `${prefix}-extra-h`, heading));
  const list = box.appendChild(kit.el('div', `${prefix}-extra-list`));
  return {
    update(items) {
      box.hidden = !items.length;
      kit.html(list, items.map((t) => `<button type="button" class="${prefix}-pe st-${t.level}" data-item="${escapeHtml(t.id)}" data-st="${t.level}" data-action="openTargetHistory" data-arg="${escapeHtml(t.id)}" title="${escapeHtml(t.title)}">`
        + `${icon(t.icon, 'sm')}<span data-v="name">${escapeHtml(t.name)}</span><b class="${prefix}-n" data-v="ms">${escapeHtml(t.ms)}</b></button>`).join(''));
    }
  };
}

// The last `count` probes as block characters, ▁ to █ on the highest of them, a lost probe as ×.
const BLOCKS = '▁▂▃▄▅▆▇█';
function blockTrace(values, count) {
  const last = (values || []).slice(-count);
  const top = Math.max(1, ...last.filter((v) => v !== null && v !== undefined));
  return last.map((v) => (v === null || v === undefined ? '×' : BLOCKS[Math.min(7, Math.floor((v / top) * 7.99))])).join('');
}

export const parts = { verdict, notice, chain, extras, legend, nodeShell, paintNode, verdictLine, hopFields, extraChips, blockTrace };

export default {
  parts,
  mount(root) {
    const p = {
      verdict: verdict(root.querySelector('.verdict')), notice: notice(root.querySelector('.path-banner')),
      chain: chain(root.querySelector('.chain')), extras: extras(root.querySelector('.path-extra')), legend: legend(root.querySelector('.hero-foot'))
    };
    return {
      update(vm, prev = {}) {
        if (vm.verdict !== prev.verdict) p.verdict.update(vm.verdict);
        if (vm.banner !== prev.banner) p.notice.update(vm.banner);
        if (vm.path && vm.path !== prev.path) {
          p.chain.update(vm.path);
          p.extras.update(vm.path.extras);
        }
        if (vm.busyLegend !== prev.busyLegend) p.legend.update(vm.busyLegend);
      },
      unmount() { p.chain.unmount(); }
    };
  }
};
