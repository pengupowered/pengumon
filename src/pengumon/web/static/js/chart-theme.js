// Colours for canvas drawing (Chart.js, the sparklines, the channel heatmap): canvas cannot read CSS variables, so
// these read the design tokens (tokens.css) at the moment of drawing. Chart.js's defaults are set from them at start
// and after every theme change (chrome.js), and every chart is redrawn; a module gives its charts series colours
// only, never text, grid or tooltip colours.

export const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// The categorical series colours in their fixed order (tokens.css --s1..--s4), for charts whose series are not targets.
export const series = (i) => token(`--s${(i % 4) + 1}`);

// Chart.js defaults from the tokens: text, grid, font, tooltip. Called at start and after every theme change.
export function applyChartTheme() {
  if (typeof Chart === 'undefined') return;
  const d = Chart.defaults;
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
  Object.values(Chart.instances).forEach((c) => c.update('none'));
}
