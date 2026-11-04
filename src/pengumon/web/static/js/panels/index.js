// The panels and dialogs on this page are the folders the server found (pengumon.ui: static/panels/<id>/ and
// static/dialogs/<id>/), listed on <body data-modules>. They are loaded here with dynamic import(): the dialogs first
// (they register their actions), then the panels in their default order. A panel is one folder whose panel.js
// default-exports the contract
//   { id, title, tab, icon, core?, available(ctx) | instances(ctx), reason?(ctx), prepare?(ctx), mount?(section, ctx[, instance]) }
// with `id` the folder's name and its section's data-panel. No panel or dialog is named here; one that fails to load
// is left out and logged, and the rest of the page works.
//
// A dialog whose folder has a dialog.json with a "lazy" entry is not loaded at start (most visits never open most
// dialogs: about a third of the page's script). The entry says what opens it: the click actions that open it
// ("actions"), the modals that the generic openModal action opens ("modals"), and a location hash ("hash"). Its module is
// imported the first time one of those is used, and ahead of the click when the pointer or the keyboard focus reaches
// the control (so the click rarely waits). pengumon.web.pages puts them on <body data-modules> as `lazy`;
// tests/test_frontend.py checks that every way in is named.
import { registerLazy } from '../dispatch.js';

function list() {
  try {
    return JSON.parse(document.body.dataset.modules || '{}');
  } catch (e) {
    return {};
  }
}

const base = new URL('../../', import.meta.url); // the asset root: /static/v/<hash>/

async function load(path) {
  try {
    return await import(new URL(path, base).href);
  } catch (e) {
    console.error(`could not load ${path}: ${e && e.message ? e.message : e}`);
    return null;
  }
}

// One import per lazy dialog, started by the first thing that needs it; a failed one can be tried again.
function lazyDialog(entry) {
  let loading = null;
  const start = () => {
    loading ||= import(new URL(entry.module, base).href).catch((e) => {
      loading = null;
      console.error(`could not load ${entry.module}: ${e && e.message ? e.message : e}`);
      throw e;
    });
    return loading;
  };
  const actions = entry.actions || [];
  const modals = entry.modals || [];
  registerLazy('click', actions, start);
  const mine = (el) => {
    const control = el.closest('[data-action]');
    if (!control) return false;
    const name = control.getAttribute('data-action');
    return actions.includes(name) || (name === 'openModal' && modals.includes(control.getAttribute('data-arg')));
  };
  const ahead = (ev) => { if (ev.target instanceof Element && mine(ev.target)) start().catch(() => {}); };
  ['pointerover', 'focusin', 'touchstart', 'click'].forEach((type) => document.addEventListener(type, ahead, { capture: true, passive: true }));
  if (entry.hash) {
    const check = () => { if (location.hash === `#${entry.hash}`) start().catch(() => {}); };
    window.addEventListener('hashchange', check);
    check(); // the module opens its dialog itself when it finds the hash
  }
}

export async function loadModules() {
  const { dialogs = [], lazy = [], panels = [] } = list();
  lazy.forEach(lazyDialog);
  await Promise.all(dialogs.map(load));
  const modules = await Promise.all(panels.map(load));
  return modules.filter((m) => m && m.default).map((m) => m.default);
}
