// Event delegation: the page has no inline handlers (the CSP forbids them). An element names its action in
// data-action (click), data-input, data-change or data-submit, and only names registered here can run.
const ATTRS = { click: 'data-action', input: 'data-input', change: 'data-change', submit: 'data-submit' };
const maps = { click: {}, input: {}, change: {}, submit: {} };

// `kind` is the event: 'click', 'input', 'change' or 'submit'. Handlers get (element, event). Every module registers the
// actions its own markup names; a name taken twice would silently replace the first handler, so it is refused.
export function registerActions(kind, actions) {
  if (!maps[kind]) throw new Error(`registerActions: unknown event kind "${kind}"`);
  for (const name of Object.keys(actions)) {
    if (name in maps[kind] && !stubs.has(maps[kind][name])) throw new Error(`registerActions: ${kind} action "${name}" is already registered`);
  }
  Object.assign(maps[kind], actions); // a stand-in (below) gives way to the real handler
}

// A lazy dialog's opening actions, before its module is loaded: the stand-in loads the module (`load` returns a promise,
// panels/index.js) and runs the real handler, which that module registered in its place.
const stubs = new Set();
export function registerLazy(kind, names, load) {
  for (const name of names) {
    if (name in maps[kind]) throw new Error(`registerLazy: ${kind} action "${name}" is already registered`);
    const stub = (el, ev) => {
      load().then(() => {
        const real = maps[kind][name];
        if (real && real !== stub) real(el, ev);
      }).catch(() => { /* load() has logged why */ });
    };
    stubs.add(stub);
    maps[kind][name] = stub;
  }
}

function delegate(type, attr, actions) {
  document.addEventListener(type, (ev) => {
    const el = ev.target instanceof Element ? ev.target.closest(`[${attr}]`) : null;
    if (!el) return;
    const fn = actions[el.getAttribute(attr)];
    if (fn) fn(el, ev);
  });
}

Object.entries(ATTRS).forEach(([type, attr]) => delegate(type, attr, maps[type]));
