// Themes: which one the page shows, light or dark, a theme's animated effect, and each card's own colors
// (docs/themes.md). A theme is a folder under static/themes/; the server lists them on <html data-looks> and in
// GET /api/themes, and js/theme-boot.js has already put this browser's choice on <html data-look> (and data-theme)
// before the first paint, and linked that theme's stylesheet (its tokens, skin and card views' styles). tokens.css and
// that stylesheet read those two attributes. Another theme's stylesheet is linked when it is picked, and the page
// changes to it once the sheet and the theme's own card views (loadView; js/cards.js mounts them) have loaded: no moment
// in the new theme without its styles, nor a card in the old theme's view without them. The old sheet is then unlinked.
//
// This browser's choices (localStorage): `pengumon.look` (a theme id; none: the default from Settings),
// `pengumon.theme` (light or dark; none: the system's) and `pengumon.effects` ("on" or "off"; none: Settings' choice).
// The header's theme button opens a menu with all three (fillThemeMenu, called by chrome.js when it opens).
//
// Every change goes through `changed()`: the chart colors are read again (chart-theme.js), the cards' colors are
// painted, the effect is started or stopped, and a `themechange` event tells modules that draw on canvas to redraw.
//
// Effects: a theme's effect.js is an ES module whose default export is { start(layer, ctx) -> stop }. `layer` is
// #fxLayer, a full-viewport element behind the page; ctx gives `tokens()` (the current tokens by name), `fps` (15) and
// `onFrame(cb, fps)`: one shared frame loop, capped at 15 frames a second (or the lower `fps` the effect asks for: every
// frame costs the page a style and paint pass, about 2 to 4 ms of main thread in the headless test browser) and paused
// while the tab is hidden. An effect
// runs only while the theme has one, effects are on (Settings and this browser) and the system does not ask for
// reduced motion.

import { registerActions } from './dispatch.js';
import { applyChartTheme, token } from './chart-theme.js';
import { escapeHtml } from './escape.js';

const LOOK_KEY = 'pengumon.look';
const MODE_KEY = 'pengumon.theme';
const FX_KEY = 'pengumon.effects';
const FPS = 15;
// What a card's own colors set (Layout): the accent and the series colors; a theme's palette brings its accent fill,
// the text on it and the focus ring too.
export const CARD_TOKENS = ['--accent', '--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
const PALETTE_TOKENS = [...CARD_TOKENS, '--accent-bg', '--on-accent', '--focus'];

const root = document.documentElement;
const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

function read(key) {
  try { return localStorage.getItem(key) || ''; } catch (e) { return ''; }
}
function write(key, value) {
  try {
    if (value) localStorage.setItem(key, value); else localStorage.removeItem(key);
  } catch (e) {
    // not kept; applies until the page is closed
  }
}

// ---------- the themes ----------
let looks = [];
try { looks = JSON.parse(root.dataset.looks || '[]'); } catch (e) { looks = []; }
let full = null; // GET /api/themes, once read (loadThemes)

export const themeList = () => looks;
export const lookInfo = (id) => looks.find((t) => t.id === id) || null;
export const currentLook = () => root.dataset.look || 'default';
const defaultLook = () => root.dataset.lookDefault || 'default';
const isDark = () => (root.dataset.theme ? root.dataset.theme === 'dark' : systemDark.matches);
const forcedMode = (look) => (look && look.modes.length === 1 ? look.modes[0] : '');

// The full list (descriptions, the colors of each theme), read once; `fresh` reads it again (after a Save).
export async function loadThemes(fresh = false) {
  if (full && !fresh) return full;
  const res = await fetch('/api/themes', { cache: 'no-store' });
  if (!res.ok) throw new Error(`/api/themes answered ${res.status}`);
  full = await res.json();
  // the server's own page list (pengumon.web.themes.page_list, as on <html data-looks>): every field the modules read
  // after this (the stylesheet, the cards' views, the chart style) is there because one function builds both
  looks = full.looks;
  root.dataset.looks = JSON.stringify(looks);
  root.dataset.lookDefault = full.default;
  root.dataset.effects = full.effects ? 'on' : 'off';
  return full;
}

// ---------- applying ----------
// The theme's stylesheet (`css` in the page list), linked where theme-boot.js linked the first one: after the page's own
// stylesheets. Returns a Promise that settles once it has loaded (or failed: the theme then shows with the standard
// styles), or null when there is nothing to wait for: no stylesheet, or one already there (theme-boot.js's link has
// settled before any module runs: module scripts wait for the stylesheets in <head>).
const sheets = () => [...document.querySelectorAll('link[data-look-css]')];
const pending = new WeakMap(); // a link added here -> the Promise of its load, until it settles
function linkSheet(look) {
  if (!look || !look.css) return null;
  const there = sheets().find((l) => l.dataset.lookCss === look.id);
  if (there) return pending.get(there) || null;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = look.css;
  link.dataset.lookCss = look.id;
  const after = sheets().pop() || document.querySelector('script[src$="/js/theme-boot.js"]');
  if (after) after.after(link); else document.head.append(link);
  const loaded = new Promise((resolve) => {
    const done = () => { pending.delete(link); resolve(); };
    link.addEventListener('load', done, { once: true });
    link.addEventListener('error', done, { once: true });
  });
  pending.set(link, loaded);
  return loaded;
}

// The theme the page shows: this browser's choice, else the default from Settings.
const chosenLook = () => lookInfo(read(LOOK_KEY)) || lookInfo(defaultLook()) || lookInfo('default');

// A theme's own view of a card (themes/<id>/panels/<panel>.js), as the Promise of its module's default export: loaded
// once per page; null when it cannot be loaded or has no mount (the card then keeps its standard view).
const views = new Map(); // view URL -> that Promise
export function loadView(url) {
  if (!views.has(url)) {
    views.set(url, import(url).then((m) => (m.default && typeof m.default.mount === 'function' ? m.default : null)).catch((e) => {
      console.warn(`A theme's card view could not be loaded (${url}): ${e && e.message ? e.message : e}`);
      return null;
    }));
  }
  return views.get(url);
}

let applySeq = 0;
function apply() {
  const look = chosenLook();
  const seq = ++applySeq;
  const waits = [linkSheet(look)];
  if (look && look.id !== currentLook()) waits.push(...Object.values(look.views || {}).map(loadView)); // a switch: its card views too
  const waiting = waits.filter(Boolean);
  if (waiting.length) Promise.all(waiting).then(() => { if (seq === applySeq) show(look); });
  else show(look);
}

function show(look) {
  root.dataset.look = look ? look.id : 'default';
  const mode = forcedMode(look) || read(MODE_KEY);
  if (mode === 'light' || mode === 'dark') root.dataset.theme = mode; else delete root.dataset.theme;
  sheets().forEach((l) => { if (l.dataset.lookCss !== root.dataset.look) l.remove(); }); // only the theme in use
  changed();
}

function changed() {
  const meta = document.querySelector('meta[name="theme-color"]');
  applyChartTheme(); // forgets the cached tokens too
  if (meta) meta.content = token('--bg');
  paintCards();
  runEffect();
  document.dispatchEvent(new CustomEvent('themechange'));
}

export function setLook(id) {
  write(LOOK_KEY, id && lookInfo(id) ? id : '');
  apply();
}

function setMode(mode) {
  write(MODE_KEY, mode === 'light' || mode === 'dark' ? mode : '');
  apply();
}

function setEffects(choice) {
  write(FX_KEY, choice === 'on' || choice === 'off' ? choice : '');
  runEffect();
}

// After a Save in Settings (or a revision from elsewhere): the default theme, the effects switch and the custom
// themes may have changed. The custom themes' token blocks are rebuilt from their checked values. The page is themed
// again only when what it shows changed (a Save of anything else leaves the charts and cards alone).
export async function refreshThemes() {
  const was = { look: chosenLook()?.id, effects: root.dataset.effects };
  let css = false;
  try {
    const data = await loadThemes(true);
    const style = document.getElementById('customThemeCss');
    const text = data.themes.filter((t) => t.custom).map((t) => customCss(t.theme)).join('');
    if (style && style.textContent !== text) { style.textContent = text; css = true; }
  } catch (e) {
    return; // the page keeps what it has
  }
  if (css || root.dataset.effects !== was.effects || chosenLook()?.id !== was.look) apply();
}

const SAFE_VALUE = /^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]+\)|\d+(\.\d+)?px)$/;
const SAFE_NAME = /^--[a-z0-9-]+$/;
function block(selector, scheme, tokens) {
  const body = Object.entries(tokens || {}).filter(([n, v]) => SAFE_NAME.test(n) && SAFE_VALUE.test(String(v).trim())).map(([n, v]) => ` ${n}: ${String(v).trim()};`).join('');
  return `${selector} { color-scheme: ${scheme};${body} }\n`;
}
// The same blocks as the server writes (pengumon.web.themes.theme_css), for a custom theme (colors and sizes only).
function customCss(theme) {
  if (!/^custom-[a-z0-9-]+$/.test(theme.id)) return '';
  const look = `:root[data-look="${theme.id}"]`;
  if (theme.modes.length === 1) return block(look, theme.modes[0], theme.tokens[theme.modes[0]]);
  return block(look, 'dark', theme.tokens.dark)
    + `@media (prefers-color-scheme: light) {\n  ${block(`${look}:not([data-theme="dark"])`, 'light', theme.tokens.light)}}\n`
    + block(`${look}[data-theme="light"]`, 'light', theme.tokens.light);
}

// ---------- a card's own colors (Layout) ----------
let cards = {}; // panel id -> { theme } | { custom: {token: '#rrggbb'} }
let cardsJson = '{}';

// The cards' own colors (Layout, on every layout change): the cards are painted and the charts redrawn only when the
// colors changed.
export function setCardColors(map) {
  const next = map && typeof map === 'object' ? map : {};
  const json = JSON.stringify(next);
  if (json === cardsJson) return;
  cards = next;
  cardsJson = json;
  paintCards();
  applyChartTheme();
  document.dispatchEvent(new CustomEvent('themechange'));
}

// black or white, whichever reads better on `hex`
function onColor(hex) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return '#ffffff';
  const lin = (c) => { const v = parseInt(c, 16) / 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const l = 0.2126 * lin(m[1]) + 0.7152 * lin(m[2]) + 0.0722 * lin(m[3]);
  return (l + 0.05) / 0.05 >= 1.05 / (l + 0.05) ? '#000000' : '#ffffff';
}

// The custom properties a card gets: none for the page theme's colors.
function cardVars(spec, data = full) {
  if (!spec) return {};
  if (spec.custom && Object.keys(spec.custom).length) {
    const vars = {};
    CARD_TOKENS.forEach((n) => { if (/^#[0-9a-f]{6}$/i.test(spec.custom[n] || '')) vars[n] = spec.custom[n]; });
    if (vars['--accent']) {
      vars['--accent-bg'] = `color-mix(in srgb, ${vars['--accent']} 14%, transparent)`;
      vars['--on-accent'] = onColor(vars['--accent']);
      vars['--focus'] = vars['--accent'];
    }
    return vars;
  }
  const entry = spec.theme && data ? data.themes.find((t) => t.id === spec.theme) : null;
  if (!entry) return {};
  const tokens = entry.theme.tokens[isDark() ? 'dark' : 'light'] || entry.theme.tokens[entry.modes[0]];
  return Object.fromEntries(PALETTE_TOKENS.filter((n) => tokens[n]).map((n) => [n, tokens[n]]));
}

let paintSeq = 0;
async function paintCards() {
  const seq = ++paintSeq;
  const waited = Object.values(cards).some((c) => c && c.theme) && !full;
  if (waited) {
    try { await loadThemes(); } catch (e) { return; }
    if (seq !== paintSeq) return;
  }
  let painted = false;
  document.querySelectorAll('[data-panel]').forEach((section) => {
    const vars = cardVars(cards[section.dataset.panel]);
    (section.dataset.colorVars || '').split(' ').filter(Boolean).forEach((n) => section.style.removeProperty(n));
    Object.entries(vars).forEach(([n, v]) => section.style.setProperty(n, v));
    const names = Object.keys(vars);
    if (names.length) {
      section.dataset.colorVars = names.join(' ');
      section.dataset.colors = cards[section.dataset.panel].theme || 'custom';
      painted = true;
    } else {
      delete section.dataset.colorVars;
      delete section.dataset.colors;
    }
  });
  if (waited && painted) { applyChartTheme(); document.dispatchEvent(new CustomEvent('themechange')); } // the charts drew before the colors came
}

// ---------- the effect ----------
const frames = new Map(); // callback -> its frames a second
let timer = 0;
let raf = 0;
let last = 0;
let fx = null; // { url, stop }
let fxSeq = 0;
const layer = () => document.getElementById('fxLayer');

function tick(now) {
  raf = 0;
  const dt = last ? now - last : 0;
  last = now;
  frames.forEach((_, cb) => cb(now, dt));
  schedule();
}

// One frame every 1000/15 ms at most: a timer, then the browser's next frame (neither runs while the tab is hidden).
function schedule() {
  if (timer || raf || !frames.size || document.hidden) return;
  const wait = Math.max(0, 1000 / Math.max(...frames.values()) - (performance.now() - last));
  timer = setTimeout(() => { timer = 0; raf = requestAnimationFrame(tick); }, wait);
}

function pause() {
  clearTimeout(timer);
  cancelAnimationFrame(raf);
  timer = 0;
  raf = 0;
  last = 0;
}

function effectWanted(look) {
  if (!look || !look.effect || reducedMotion.matches) return false;
  const mine = read(FX_KEY);
  if (mine) return mine === 'on';
  return root.dataset.effects !== 'off' && look.effect_on !== false;
}

const effectsOn = () => (read(FX_KEY) ? read(FX_KEY) === 'on' : root.dataset.effects !== 'off');

function stopEffect() {
  fxSeq++;
  pause();
  frames.clear();
  if (fx && typeof fx.stop === 'function') fx.stop();
  fx = null;
  const el = layer();
  if (!el) return;
  el.replaceChildren();
  delete el.dataset.fx;
  delete el.dataset.state;
}

// Every token by name, read when asked ("--accent" -> "#4be3c3"): an effect draws in the theme's colors.
const tokenView = new Proxy({}, { get: (_, name) => (typeof name === 'string' && name.startsWith('--') ? token(name) : undefined) });

async function runEffect() {
  const look = lookInfo(currentLook());
  const url = effectWanted(look) ? look.effect : '';
  if (fx && fx.url === url) return;
  stopEffect();
  const el = layer();
  if (!url || !el) return;
  const seq = fxSeq;
  let mod;
  try {
    mod = await import(url);
  } catch (e) {
    console.error(`The theme's effect could not be loaded: ${e.message}`);
    return;
  }
  if (seq !== fxSeq || !mod.default || typeof mod.default.start !== 'function') return;
  const ctx = {
    fps: FPS,
    tokens: () => tokenView,
    onFrame: (cb, fps = FPS) => {
      frames.set(cb, Math.min(FPS, Math.max(1, Number(fps) || FPS)));
      schedule();
      return () => frames.delete(cb);
    }
  };
  try {
    fx = { url, stop: mod.default.start(el, ctx) };
  } catch (e) { // an effect that throws in start() is stopped (what it set going is cleared); the theme itself carries on
    console.error(`The theme's effect could not be started: ${e.message}`);
    stopEffect();
    return;
  }
  el.dataset.fx = look.id;
  el.dataset.state = document.hidden ? 'paused' : 'running';
}

document.addEventListener('visibilitychange', () => {
  const el = layer();
  if (document.hidden) pause(); else schedule();
  if (el && fx) el.dataset.state = document.hidden ? 'paused' : 'running';
});
reducedMotion.addEventListener('change', () => runEffect());
systemDark.addEventListener('change', () => { if (!root.dataset.theme) changed(); });

// ---------- the header's theme menu ----------
const item = (role, checked, action, arg, text, extra = '') => `<button type="button" role="${role}" aria-checked="${checked}" data-action="${action}" data-arg="${escapeHtml(arg)}"${extra}><span class="tm-mark" aria-hidden="true"></span><span class="tm-text">${text}</span></button>`;
const modeWords = (t) => (t.modes.length === 1 ? `<span class="tm-note">${t.modes[0]} only</span>` : '');

export function fillThemeMenu(menu) {
  const mine = read(LOOK_KEY) && lookInfo(read(LOOK_KEY)) ? read(LOOK_KEY) : '';
  const look = lookInfo(currentLook());
  const def = lookInfo(defaultLook());
  const forced = forcedMode(look);
  const mode = read(MODE_KEY);
  const parts = ['<div class="tm-h" role="presentation">Theme for this browser</div>',
    item('menuitemradio', !mine, 'pickLook', '', `Use the default <span class="tm-note">${escapeHtml(def ? def.name : 'Pengumon')}</span>`)];
  looks.forEach((t) => parts.push(item('menuitemradio', mine === t.id, 'pickLook', t.id, `${escapeHtml(t.name)}${modeWords(t)}`)));
  parts.push('<hr><div class="tm-h" role="presentation">Light or dark</div>');
  const off = forced ? ' disabled' : '';
  [['', 'Like the system'], ['light', 'Light'], ['dark', 'Dark']].forEach(([m, words]) => {
    parts.push(item('menuitemradio', forced ? m === forced : mode === m, 'pickMode', m, words, off));
  });
  if (forced) parts.push(`<p class="tm-why">${escapeHtml(look.name)} has a ${forced} look only.</p>`);
  parts.push('<hr>');
  const why = reducedMotion.matches ? 'Off while the system asks for reduced motion.' : look && look.effect ? '' : `${escapeHtml(look ? look.name : 'This theme')} has no effect.`;
  parts.push(item('menuitemcheckbox', effectsOn(), 'toggleEffects', effectsOn() ? 'off' : 'on', 'Animated effects'));
  if (why) parts.push(`<p class="tm-why">${why}</p>`);
  menu.innerHTML = parts.join('');
}

registerActions('click', {
  pickLook: (el) => setLook(el.dataset.arg),
  pickMode: (el) => setMode(el.dataset.arg),
  toggleEffects: (el) => setEffects(el.dataset.arg)
});

// At start (chrome.js): theme-boot.js has set the attributes; this reads them once for the charts, cards and effect.
export function initThemes() {
  apply();
}
