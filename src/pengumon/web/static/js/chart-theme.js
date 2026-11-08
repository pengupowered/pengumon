// Colors for canvas drawing (Chart.js, the sparklines, the channel heatmap): canvas cannot read CSS variables, so
// these read the design tokens (tokens.css) at the moment of drawing. Chart.js's defaults are set from them at start
// and after every theme change (chrome.js), and every chart is redrawn; a module gives its charts series colors
// only, never text, grid or tooltip colors.

// Reading a token resolves it through getComputedStyle (a style recalculation when anything is dirty), and the charts
// and sparklines read the same few tokens on every message, so the values are kept until the theme changes
// (applyChartTheme, below, empties the cache; it runs at start and after every theme change). An empty result
// (a token that is not defined yet) is never kept.
//
// A card can have colors of its own (Layout, themes.js: its accent and series colors as custom properties on its
// <section data-colors>): pass an element of the card as `el` and the token is read there, so its chart follows.
let tokenCache = new Map();
let cardCache = new WeakMap(); // card section -> Map
export const clearTokenCache = () => { tokenCache = new Map(); cardCache = new WeakMap(); };
export function token(name, el) {
  const card = el && el.closest ? el.closest('[data-colors]') : null;
  let cache = tokenCache;
  if (card) {
    cache = cardCache.get(card);
    if (!cache) { cache = new Map(); cardCache.set(card, cache); }
  }
  let v = cache.get(name);
  if (v === undefined) {
    v = getComputedStyle(card || document.documentElement).getPropertyValue(name).trim();
    if (v) cache.set(name, v);
  }
  return v;
}

// The categorical series colors in their fixed order (tokens.css --s1..--s4), for charts whose series are not targets;
// `el`: an element of the card the chart is in (its own colors, see token).
export const series = (i, el) => token(`--s${(i % 4) + 1}`, el);

// ---------- a theme's chart style (theme.json "chart", docs/themes.md "Chart style") ----------
// {line_width, curve, grid, points, fill, spark}, each optional; null: the charts as their modules draw them. It reshapes
// every Chart.js line chart (a plugin, below, applies it on each update), so a module keeps giving its datasets its own
// widths and tensions and never asks for the style itself; `spark` is read by sparkline.js.
let style = null;
export const chartStyle = () => style;

function readStyle() {
  const root = document.documentElement;
  try {
    const look = JSON.parse(root.dataset.looks || '[]').find((t) => t && t.id === root.dataset.look);
    return look && look.chart && typeof look.chart === 'object' ? look.chart : null;
  } catch (e) {
    return null;
  }
}

const own = new WeakMap(); // a dataset -> what its module gave it, before the style changed it
const ownGrid = new WeakMap(); // a chart -> {scale id: its module's grid.display}
const HEX6 = /^#[0-9a-f]{6}$/i;
const stylePlugin = {
  id: 'themeChartStyle',
  beforeUpdate(chart) {
    const s = style || {};
    chart.data.datasets.forEach((d) => {
      if ((d.type || chart.config.type) !== 'line') return;
      let o = own.get(d);
      if (!o) {
        o = { borderWidth: d.borderWidth, tension: d.tension, stepped: d.stepped, pointRadius: d.pointRadius, fill: d.fill, backgroundColor: d.backgroundColor };
        own.set(d, o);
      }
      const stroked = typeof o.borderWidth === 'number' && o.borderWidth > 0; // a band or an invisible helper line stays as it is
      d.borderWidth = stroked && s.line_width ? Math.round(o.borderWidth * s.line_width * 5) / 10 : o.borderWidth; // in proportion to the main lines' 2 px
      d.tension = stroked && !d.ownCurve && (s.curve === 'straight' || s.curve === 'stepped') ? 0 : o.tension;
      d.stepped = stroked && !d.ownCurve && s.curve === 'stepped' ? 'middle' : o.stepped;
      d.pointRadius = stroked && s.points ? s.points : o.pointRadius;
      const wash = stroked && s.fill && !o.fill && HEX6.test(d.borderColor || '');
      d.fill = wash ? 'origin' : o.fill;
      d.backgroundColor = wash ? `${d.borderColor}1f` : o.backgroundColor;
    });
    const scales = chart.options.scales || {};
    if (!ownGrid.has(chart)) ownGrid.set(chart, Object.fromEntries(Object.entries(scales).map(([id, sc]) => [id, sc.grid ? sc.grid.display : undefined])));
    const mine = ownGrid.get(chart);
    Object.entries(scales).forEach(([id, sc]) => {
      if (!sc.grid) return;
      const was = id in mine ? mine[id] : sc.grid.display;
      sc.grid.display = s.grid === false ? false : was;
    });
  }
};
if (typeof Chart !== 'undefined') Chart.register(stylePlugin);

// Chart.js defaults from the tokens: text, grid, font, tooltip; and the theme's chart style. Called at start and after
// every theme change.
export function applyChartTheme() {
  clearTokenCache();
  style = readStyle();
  if (typeof Chart === 'undefined') return;
  const d = Chart.defaults;
  // Chart.js copies the scale defaults into each chart's own options when the chart is made, so a chart made in the other
  // theme keeps the old text, grid and axis colors however the defaults change; remember what they were to swap them below.
  const swap = new Map();
  const remember = (was, now) => { if (typeof was === 'string' && was && was !== now && !swap.has(was)) swap.set(was, now); };
  remember(d.color, token('--ink-3'));
  remember(d.borderColor, token('--line'));
  remember(d.scale?.grid?.color, token('--line'));
  remember(d.scale?.border?.color, token('--line-strong'));
  d.color = token('--ink-3');
  d.borderColor = token('--line');
  d.font.family = token('--font');
  d.font.size = 11;
  d.scale.grid.color = token('--line');
  d.scale.border.color = token('--line-strong');
  d.plugins.legend.labels.color = token('--ink-2');
  d.plugins.legend.labels.boxWidth = 10;
  d.plugins.legend.labels.boxHeight = 10;
  Object.assign(d.plugins.tooltip, {
    backgroundColor: token('--surface-1'), titleColor: token('--ink-1'), bodyColor: token('--ink-2'),
    borderColor: token('--line-strong'), borderWidth: 1, padding: 10, cornerRadius: 10, boxPadding: 4
  });
  Object.values(Chart.instances).forEach((c) => {
    if (swap.size) {
      Object.values(c.config.options.scales || {}).forEach((scale) => {
        ['grid', 'border', 'ticks', 'title'].forEach((part) => {
          const o = scale[part];
          if (o && swap.has(o.color)) o.color = swap.get(o.color);
        });
      });
    }
    c.update('none');
  });
}
