// The dialog's markup, built on first use (so the page template stays small).

import { icon } from '../../js/dom.js';
import { keepKeyboard } from '../../js/modal.js';
import { RANGES, labels } from './state.js';

function make(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

export function ensureModal() {
  let modal = document.getElementById('targetHistModal');
  if (modal) return modal;
  modal = make('div', 'modal');
  modal.id = 'targetHistModal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'targetHistTitle');
  const content = make('div', 'modal-content');
  content.style.setProperty('--w', '940px');
  const head = make('header', 'modal-h');
  head.innerHTML = icon('globe');
  const h2 = head.appendChild(make('h2', 'modal-t', 'Target history'));
  h2.id = 'targetHistTitle';
  h2.tabIndex = -1;
  head.appendChild(make('span', 'modal-m')).id = 'targetHistMeta';
  const tools = head.appendChild(make('div', 'tools'));
  const ranges = tools.appendChild(make('span', 'seg'));
  ranges.setAttribute('role', 'group');
  ranges.setAttribute('aria-label', 'Range');
  ranges.id = 'targetHistRanges';
  RANGES.forEach(([seconds, label]) => {
    const b = make('button', '', label);
    b.type = 'button';
    b.dataset.action = 'setTargetHistoryRange';
    b.dataset.arg = String(seconds);
    ranges.appendChild(b);
  });
  const close = make('button', 'btn icon m-close');
  close.innerHTML = icon('close');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  close.dataset.action = 'closeModal';
  close.dataset.arg = 'targetHistModal';
  head.append(close);
  const body = make('div', 'modal-b');
  const sw = body.appendChild(make('div', 'seg tgt-switch'));
  sw.id = 'targetHistSwitch';
  sw.setAttribute('role', 'group');
  sw.setAttribute('aria-label', 'Show');
  sw.hidden = true;
  const stats = body.appendChild(make('div', 'tgt-stats'));
  stats.id = 'targetHistStats';
  stats.innerHTML = labels().map((l) => `<div class="stat"><span class="lbl">${l}</span><span class="val num">–</span></div>`).join('');
  const now = body.appendChild(make('p', 'fine tgt-now'));
  now.id = 'targetHistNow';
  now.hidden = true;
  const latSect = body.appendChild(make('section', 'sect'));
  latSect.innerHTML = `<h3 class="section-t">${icon('activity')}Latency<span class="muted" id="targetHistNote">p10 to p90 band, loss underneath</span></h3>`;
  const latBox = latSect.appendChild(make('div', 'chart-box'));
  latBox.style.setProperty('--h', '210px');
  const lat = latBox.appendChild(document.createElement('canvas'));
  lat.id = 'targetHistLatency';
  lat.setAttribute('role', 'img');
  lat.setAttribute('aria-label', 'Latency over the chosen range');
  const lossBox = latSect.appendChild(make('div', 'chart-box tgt-loss'));
  lossBox.style.setProperty('--h', '80px');
  const loss = lossBox.appendChild(document.createElement('canvas'));
  loss.id = 'targetHistLoss';
  loss.setAttribute('role', 'img');
  loss.setAttribute('aria-label', 'Lost probes over the chosen range');
  const status = latSect.appendChild(make('p', 'fine'));
  status.id = 'targetHistStatus';
  status.setAttribute('role', 'status');
  const events = latSect.appendChild(make('ul', 'tgt-events'));
  events.id = 'targetHistEvents';
  events.setAttribute('aria-label', 'Changes to this target');
  events.hidden = true;
  const upSect = body.appendChild(make('section', 'sect'));
  upSect.innerHTML = `<h3 class="section-t">${icon('grid')}Last 24 hours<span class="muted" id="targetHistUpNote">15-minute blocks</span></h3><div id="targetHistStrip"></div>`;
  const foot = make('footer', 'modal-f');
  foot.innerHTML = '<button type="button" class="btn primary" data-action="closeModal" data-arg="targetHistModal">Done</button>';
  content.append(head, body, foot);
  modal.appendChild(content);
  document.body.appendChild(modal);
  keepKeyboard(modal, '.modal-t');
  return modal;
}
