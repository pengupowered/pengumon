// Hop details, the standard view: a table with a row per target (name, loss chip, latency range bar, last, jitter, loss
// since start, sparkline), drawn from model.js's view model. Rows are created once per target and patched in place every
// tick, so the page does not re-layout (and a hover or selection survives) once a second. On a narrow card the table's
// rows become cards (components.css .dlist). docs/themes.md, "Drawing a card".

import { kit } from '../../js/cards.js';
import { getTargetInfo } from '../../js/targets.js';
import { setClass, setText } from '../../js/dom.js';
import { createSparkline, drawSparkline } from '../../js/sparkline.js';

const { mark } = kit;
const refs = new WeakMap(); // row element -> its parts

// One row, empty: its parts are filled by patch().
function build(r) {
  const tr = document.createElement('tr');
  tr.className = 'row-click';
  tr.innerHTML = `
    <td class="lead"><span class="primary" data-f="label"></span><span class="sub" data-f="sub"><span data-f="name"></span><span data-f="addrWrap"> · <span class="mono" data-f="addr"></span></span><span data-f="probeWrap" hidden> · <span data-f="probe"></span></span></span></td>
    <td class="aside"><span class="chip" data-f="loss"></span></td>
    <td class="wide"><span class="range" role="img" data-f="range"><i data-f="band"></i><b data-f="mark"></b></span><span class="range-n num" data-f="rangeText"></span></td>
    <td class="r num" data-l="Last"><b data-f="last"></b></td>
    <td class="r num hide-narrow" data-f="jitter"></td>
    <td class="r num hide-narrow c-total" data-f="total" title="Every probe since Pengumon started"></td>
    <td class="hide-narrow spark-td c-spark" data-f="sparkCell"></td>`;
  tr.dataset.action = 'openTargetHistory';
  tr.dataset.arg = r.id;
  tr.title = 'Show the latency and loss history';
  const p = {};
  tr.querySelectorAll('[data-f]').forEach((el) => { p[el.dataset.f] = el; });
  p.spark = createSparkline(r.spark.label);
  p.sparkCell.appendChild(p.spark);
  [['label', 'label'], ['name', 'name'], ['addr', 'address'], ['probe', 'probe'], ['loss', 'loss'], ['rangeText', 'range'], ['range', 'rangeAria'], ['last', 'last'],
    ['jitter', 'jitter'], ['total', 'total'], ['spark', 'sparkline']].forEach(([f, key]) => mark(p[f], key));
  refs.set(tr, p);
  return tr;
}

// A row from its view model, changing only what differs.
function patch(tr, r) {
  const p = refs.get(tr);
  setText(p.label, r.label);
  setText(p.name, r.name);
  setText(p.addr, r.addr);
  p.addrWrap.hidden = !r.sayAddr;
  setText(p.probe, r.probe);
  p.probeWrap.hidden = !r.probe;
  p.label.title = r.labelTitle;
  p.sub.title = r.subTitle;
  setClass(p.loss, r.loss.chip ? `chip st st-${r.loss.level}` : 'chip');
  kit.status(p.loss, r.loss.level, r.loss.text, { icon: r.loss.icon, iconClass: 'sm' });
  if (p.loss.title !== r.loss.title) p.loss.title = r.loss.title;
  const g = r.range;
  p.band.style.left = `${g.from.toFixed(1)}%`;
  p.band.style.width = g.to > g.from ? `calc(${g.to.toFixed(1)}% - ${g.from.toFixed(1)}%)` : '0';
  p.mark.hidden = g.mean === null;
  if (g.mean !== null) p.mark.style.left = `${g.mean.toFixed(1)}%`;
  setText(p.rangeText, g.text);
  if (g.aria === null) p.range.removeAttribute('aria-label'); else if (p.range.getAttribute('aria-label') !== g.aria) p.range.setAttribute('aria-label', g.aria);
  setText(p.last, r.last.text);
  setClass(p.last, r.last.tone);
  setText(p.jitter, r.jitter);
  setText(p.total, r.total);
  if (p.spark.getAttribute('aria-label') !== r.spark.label) p.spark.setAttribute('aria-label', r.spark.label);
  drawSparkline(p.spark, r.spark.values, getTargetInfo(r.spark.id).color); // the color is read here: it follows the theme
}

// The rows of the table body (a part: another view can draw them into a table of its own).
function rows(tbody) {
  return {
    update(vm) {
      if (!vm.rows.length) return;
      [...tbody.children].filter((c) => c.dataset.item === undefined).forEach((c) => c.remove()); // "Waiting for the first probes..."
      kit.list(tbody, vm.rows, { key: (r) => r.id, render: build, sig: () => '' });
      vm.rows.forEach((r, i) => patch(tbody.children[i], r));
    },
    unmount() {}
  };
}

export const parts = { rows, build, patch };

export default {
  parts,
  mount(root) {
    return rows(root.querySelector('#hopMatrixBody, tbody'));
  }
};
