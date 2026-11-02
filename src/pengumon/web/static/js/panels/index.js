// The panels and dialogs on this page are the folders the server found (pengumon.ui: static/panels/<id>/ and
// static/dialogs/<id>/), listed on <body data-modules>. They are loaded here with dynamic import(): the dialogs first
// (they register their actions), then the panels in their default order. A panel is one folder whose panel.js
// default-exports the contract
//   { id, title, tab, icon, core?, available(ctx) | instances(ctx), reason?(ctx), prepare?(ctx), mount?(section, ctx[, instance]) }
// with `id` the folder's name and its section's data-panel. No panel or dialog is named here; one that fails to load
// is left out and logged, and the rest of the page works.

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

export async function loadModules() {
  const { dialogs = [], panels = [] } = list();
  await Promise.all(dialogs.map(load));
  const modules = await Promise.all(panels.map(load));
  return modules.filter((m) => m && m.default).map((m) => m.default);
}
