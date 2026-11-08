// Synthwave: the rows of the horizon grid behind the header move slowly toward you (docs/themes.md, "Effects").
// skin.css draws the grid's rays and, while this effect is not running, still rows; this draws the moving rows on one
// canvas only as high as the grid, at 3 frames a second in the theme engine's frame loop (paused while the tab is
// hidden). Near the horizon a row hardly moves, so few frames are enough. The geometry is skin.css's: the horizon at
// HORIZON px from the top of the window, a row at HORIZON + DEPTH / depth px, the same fade.

const HORIZON = 34; // px from the top of the window (skin.css)
const DEPTH = 290; // px (skin.css)
const STEP = 0.6; // depth between two rows (skin.css)
const BAND = 240; // px: the canvas's height (lower down the rows are faint and mostly behind the cards)
const CYCLE_S = 5; // seconds for a row to move to where the next one was
const FPS = 3;
const LINE = 0.45; // a row's strength before the fade (skin.css: --sw-line)

// skin.css's cover over the grid: a haze at the horizon, clear in the middle, the page's background below
function cover(y) {
  if (y < HORIZON + 40) return 0.9 * (1 - (y - HORIZON) / 40);
  if (y < 160) return 0;
  return Math.min(1, (y - 160) / (HORIZON + DEPTH - 160));
}

export default {
  start(layer, ctx) {
    const canvas = document.createElement('canvas');
    canvas.className = 'fx-grid';
    layer.append(canvas);
    const g = canvas.getContext('2d');
    let width = 0;
    let phase = 0;
    const size = () => {
      width = layer.clientWidth;
      canvas.width = width;
      canvas.height = BAND;
    };
    size();
    window.addEventListener('resize', size);
    const off = ctx.onFrame((now, dt) => {
      phase = (phase + (Math.min(dt, 1000) / 1000) * (STEP / CYCLE_S)) % STEP;
      g.clearRect(0, 0, width, BAND);
      g.fillStyle = ctx.tokens()['--accent'];
      for (let n = 0; ; n++) {
        const z = 1 + n * STEP - phase;
        const y = HORIZON + DEPTH / z;
        if (y < HORIZON + 6) break;
        if (y >= BAND) continue;
        const a = LINE * (1 - cover(y)) * Math.min(1, (BAND - y) / 30); // and fade out before the canvas ends
        if (a <= 0.01) continue;
        g.globalAlpha = a;
        g.fillRect(0, Math.round(y), width, 1);
      }
      g.globalAlpha = 1;
    }, FPS);
    return () => {
      off();
      window.removeEventListener('resize', size);
      canvas.remove();
    };
  }
};
