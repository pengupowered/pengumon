// Colours for canvas drawing (Chart.js, the sparklines, the channel heatmap): canvas cannot read CSS variables, so
// these read the design tokens (tokens.css) at the moment of drawing. Chart.js's defaults are set from them at start
// and after every theme change (chrome.js), and every chart is redrawn; a module gives its charts series colours
// only, never text, grid or tooltip colours.

// Reading a token resolves it through getComputedStyle (a style recalculation when anything is dirty), and the charts
// and sparklines read the same few tokens on every message, so the values are kept until the theme changes
// (applyChartTheme, below, empties the cache; it runs at start and after every theme change). An empty result
// (a token that is not defined yet) is never kept.
const tokenCache = new Map();
export const clearTokenCache = () => tokenCache.clear();
export function token(name) {
  let v = tokenCache.get(name);
  if (v === undefined) {
    v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (v) tokenCache.set(name, v);
  }
  return v;
}

// The categorical series colours in their fixed order (tokens.css --s1..--s4), for charts whose series are not targets.
export const series = (i) => token(`--s${(i % 4) + 1}`);

// Chart.js defaults from the tokens: text, grid, font, tooltip. Called at start and after every theme change.
export function applyChartTheme() {
  clearTokenCache();
  if (typeof Chart === 'undefined') return;
  const d = Chart.defaults;
  // Chart.js copies the scale defaults into each chart's own options when the chart is made, so a chart made in the other
  // theme keeps the old text, grid and axis colours however the defaults change; remember what they were to swap them below.
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
