// Starship console: now and then a faint light sweeps across the header, left to right (docs/themes.md, "Effects").
// The light is one element styled by skin.css (the accent's color), on a layer of its own; a frame only moves it (a
// transform), so nothing is repainted, and between two sweeps no frame changes anything. 2 frames a second, in the
// theme engine's frame loop (paused while the tab is hidden): a console's readout steps, it does not glide.

const FPS = 2;
const SWEEP_S = 14; // seconds to cross the header
const REST_S = 16; // seconds between two sweeps
const WIDTH = 420; // px: the light's width (skin.css draws it)

export default {
  start(layer, ctx) {
    const light = document.createElement('div');
    light.className = 'fx-sweep';
    layer.append(light);
    const header = document.getElementById('topbar');
    let t = SWEEP_S; // the first sweep comes after a rest
    let shown = '';
    const size = () => {
      light.style.width = `${WIDTH}px`;
      light.style.height = `${header ? header.offsetHeight : 60}px`;
    };
    size();
    window.addEventListener('resize', size);
    const off = ctx.onFrame((now, dt) => {
      t += Math.min(dt, 1000) / 1000;
      if (t > SWEEP_S + REST_S) t = 0;
      const width = layer.clientWidth;
      const x = t <= SWEEP_S ? -WIDTH + (width + WIDTH) * (t / SWEEP_S) : -WIDTH; // resting: off to the left
      const wanted = String(Math.round(x));
      if (wanted === shown) return;
      shown = wanted;
      light.style.transform = `translate3d(${wanted}px, 0, 0)`;
    }, FPS);
    return () => {
      off();
      window.removeEventListener('resize', size);
      light.remove();
    };
  }
};
