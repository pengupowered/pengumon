// Amber CRT: a faint refresh band rolls slowly down the screen, now and then, as on an old tube (docs/themes.md,
// "Effects"). The band is one element styled by skin.css (its colors are the tokens), on a layer of its own; a frame
// only moves it (a transform) and nudges its opacity, so nothing is repainted. 3 frames a second, in the theme
// engine's frame loop (paused while the tab is hidden): each frame costs the page a style and compositing pass
// (about 6 ms of main thread in the test browser), and the soft band hides the steps.
// Photosensitivity: the band brightens what it passes by a few percent at most, takes about 14 s to cross the screen,
// and the whole page's brightness never changes at once.

const FPS = 3;
const CROSS_S = 14; // seconds to cross the screen
const REST_S = 6; // seconds between two passes
const BAND = 240; // px, as skin.css

export default {
  start(layer, ctx) {
    const band = document.createElement('div');
    band.className = 'fx-roll';
    layer.append(band);
    let t = Math.random() * CROSS_S; // start part way, so a reload does not always begin at the top
    let glow = 1;
    let shown = '';
    const off = ctx.onFrame((now, dt) => {
      t += Math.min(dt, 1000) / 1000;
      if (t > CROSS_S + REST_S) t = 0;
      const height = layer.clientHeight;
      const y = t <= CROSS_S ? -BAND + (height + BAND) * (t / CROSS_S) : height + BAND; // resting: off the screen
      if (t <= CROSS_S) glow = Math.min(1, Math.max(0.8, glow + (Math.random() - 0.5) * 0.08)); // a slight wander, never a flash
      const wanted = `${Math.round(y)}|${glow.toFixed(2)}`;
      if (wanted === shown) return;
      shown = wanted;
      band.style.transform = `translate3d(0, ${Math.round(y)}px, 0)`;
      band.style.opacity = glow.toFixed(2);
    }, FPS);
    return () => {
      off();
      band.remove();
    };
  }
};
