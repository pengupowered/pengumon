// Explanations for the dashboard's metrics. A small info button (data-action="toggleHelp" data-help="<key>") opens one
// shared popover with the text of HELP[key]; a second click, a click elsewhere, Escape, resizing or scrolling the button
// off screen closes it. The markup carries a key and nothing else; the texts come from the panels (registerHelp).

import { registerActions } from './dispatch.js';

// The texts, by key; each panel adds its own (registerHelp in its folder's help.js), so a panel's help lives with it.
export const HELP = {};

export function registerHelp(texts) {
  for (const key of Object.keys(texts)) {
    if (key in HELP) throw new Error(`registerHelp: "${key}" is already registered`);
  }
  Object.assign(HELP, texts);
}


let popover = null;
let anchor = null;

function ensurePopover() {
  if (popover) return popover;
  popover = document.createElement('div');
  popover.id = 'helpPopover';
  popover.className = 'help-popover';
  popover.setAttribute('role', 'tooltip');
  popover.hidden = true;
  document.body.appendChild(popover);
  return popover;
}

function place(button) {
  const margin = 8;
  const pop = ensurePopover();
  const r = button.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 2 * margin);
  pop.style.width = `${width}px`;
  const left = Math.min(Math.max(margin, r.left + r.width / 2 - width / 2), window.innerWidth - width - margin);
  pop.style.left = `${left}px`;
  const height = pop.offsetHeight;
  const below = r.bottom + 6;
  const top = below + height + margin <= window.innerHeight ? below : Math.max(margin, r.top - 6 - height);
  pop.style.top = `${Math.min(Math.max(margin, top), Math.max(margin, window.innerHeight - height - margin))}px`;
}

export function closeHelp() {
  if (anchor) {
    anchor.setAttribute('aria-expanded', 'false');
    anchor.removeAttribute('aria-describedby');
    anchor = null;
  }
  if (popover) popover.hidden = true;
}

export function toggleHelp(button) {
  if (anchor === button) {
    closeHelp();
    return;
  }
  const text = HELP[button.dataset.help];
  if (!text) return;
  closeHelp();
  const pop = ensurePopover();
  pop.textContent = text;
  pop.hidden = false;
  anchor = button;
  button.setAttribute('aria-expanded', 'true');
  button.setAttribute('aria-describedby', pop.id);
  place(button);
}

export function initHelp() {
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && anchor) {
      const button = anchor;
      closeHelp();
      button.focus();
    }
  });
  // A click anywhere else closes it (clicks on an info button are handled by the toggleHelp action).
  document.addEventListener('click', (ev) => {
    if (!anchor) return;
    const target = ev.target instanceof Element ? ev.target : null;
    if (target && (target.closest('.info') || target.closest('#helpPopover'))) return;
    closeHelp();
  });
  document.addEventListener('focusin', (ev) => {
    if (anchor && ev.target instanceof Element && ev.target !== anchor && !ev.target.closest('#helpPopover')) closeHelp();
  });
  window.addEventListener('resize', closeHelp);
  // Scrolling the page (or whatever holds the button) moves the button: the popover follows it, and closes once the
  // button has left the screen. (Live updates can shift the page a little, so a scroll alone must not close it.)
  window.addEventListener('scroll', (ev) => {
    const t = ev.target;
    if (!anchor || !(t === document || (t instanceof Node && t.contains(anchor)))) return;
    const r = anchor.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) closeHelp();
    else place(anchor);
  }, true);
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  toggleHelp: (el) => toggleHelp(el)
});
