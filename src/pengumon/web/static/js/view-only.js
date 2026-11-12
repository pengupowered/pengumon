// View only: a viewer without the access token sees everything and changes nothing.

// ---------- view only (PENGUMON_VIEW_WITHOUT_TOKEN) ----------
// A viewer without the access token may see everything but change nothing: /api/info `can_edit` is false for them, and
// the server refuses their POST/PUT/DELETE/PATCH (and the path export, the device credentials) whatever the page does.
// The page follows it: chrome.js calls setCanEdit() once /api/info answers, which sets <html data-view-only> (CSS dims
// the edit controls) and marks every edit control. An edit control is any element with
// `data-needs-edit` (a button, a link, a form): it gets aria-disabled and the VIEW_ONLY_TIP as its tooltip, also when it
// is drawn later (a MutationObserver, only while view only), and a click on it or a submit of it is stopped before any
// action runs (a capture listener, ahead of dispatch.js; the page then hears `viewonly`, chrome.js says why in a
// toast). Code that acts by itself (an automatic POST) asks canEdit() after `await accessKnown`. Until /api/info answers (or if it fails) the page counts as editable; the server decides.
export const VIEW_ONLY_TIP = 'View only · Sign in to edit';
export const canEdit = () => !document.documentElement.hasAttribute('data-view-only');
let accessResolve;
export const accessKnown = new Promise((resolve) => { accessResolve = resolve; });

// A form is marked through its submit buttons (its other fields stay readable); any other element itself.
const marked = (el) => (el.tagName === 'FORM' ? [...el.querySelectorAll('button[type=submit], input[type=submit]')] : [el]);

function markEditControl(el) {
  if (el.tagName === 'FORM') { marked(el).forEach(markEditControl); return; }
  if (el.dataset.editTitle === undefined) el.dataset.editTitle = el.getAttribute('title') ?? '';
  el.setAttribute('aria-disabled', 'true');
  el.title = VIEW_ONLY_TIP;
}

function unmarkEditControl(el) {
  if (el.tagName === 'FORM') { marked(el).forEach(unmarkEditControl); return; }
  if (el.dataset.editTitle === undefined) return;
  if (el.dataset.editTitle) el.title = el.dataset.editTitle; else el.removeAttribute('title');
  delete el.dataset.editTitle;
  el.removeAttribute('aria-disabled');
}

let editObserver = null;
function applyEditState(root = document) {
  const view = !canEdit();
  const els = [...(root.matches?.('[data-needs-edit]') ? [root] : []), ...root.querySelectorAll('[data-needs-edit]')];
  els.forEach(view ? markEditControl : unmarkEditControl);
}

function stopEdit(ev) {
  if (canEdit()) return;
  const el = ev.target instanceof Element ? ev.target.closest('[data-needs-edit]') : null;
  if (!el) return;
  ev.preventDefault();
  ev.stopImmediatePropagation();
  document.dispatchEvent(new CustomEvent('viewonly'));
}
document.addEventListener('click', stopEdit, true);
document.addEventListener('submit', stopEdit, true);

export function setCanEdit(on) {
  document.documentElement.toggleAttribute('data-view-only', !on);
  applyEditState();
  if (!on && !editObserver) {
    editObserver = new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => {
      if (n instanceof Element) applyEditState(n);
    })));
    editObserver.observe(document.body, { childList: true, subtree: true });
  } else if (on && editObserver) {
    editObserver.disconnect();
    editObserver = null;
  }
  accessResolve();
  document.dispatchEvent(new CustomEvent('editaccess', { detail: { canEdit: on } }));
}
