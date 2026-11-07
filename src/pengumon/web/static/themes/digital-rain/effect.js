// Digital rain: columns of characters falling down the whole viewport, behind the cards (docs/themes.md, "Effects").
// One canvas as big as the viewport, drawn in the theme engine's frame loop at 5 frames a second (paused while the tab
// is hidden): a column steps down one character at a time, as the look wants, and each frame costs the page a style and
// paint pass, so fewer frames is the main saving. Also cheap: the canvas's font is set once (setting it costs a style
// lookup), the trail fades by drawing (no CSS mask to composite), the canvas is a layer of its own (skin.css), and only
// some columns fall at a time. The cards are slightly see-through (skin.css), so the rain is drawn dim.

const GLYPHS = '0123456789ABCDEFGHJKLMNPQRSTUVWXYZ<>=+*:#$%';
const CELL = 18; // px per column and per character
const ACTIVE = 0.3; // share of columns falling at a time
const SCALE = 1; // tried 2 (canvas stretched by CSS): fewer pixels to fade but the compositor resamples the layer each frame, which cost more CPU overall
const FPS = 4;
const FADE = 0.16; // share of a trail that fades per frame

const newDrop = (height, anywhere) => ({ y: anywhere ? Math.random() * height : -CELL * (1 + Math.random() * 20), speed: 0.7 + Math.random() * 0.6, on: Math.random() < ACTIVE });

export default {
  start(layer, ctx) {
    const canvas = document.createElement('canvas');
    canvas.className = 'fx-rain';
    layer.append(canvas);
    const g = canvas.getContext('2d');
    let width = 0;
    let height = 0;
    let drops = [];
    let font = '';
    const size = () => {
      width = layer.clientWidth;
      height = layer.clientHeight;
      canvas.width = Math.ceil(width / SCALE); // also resets the context's state: the font is set again at the next frame
      canvas.height = Math.ceil(height / SCALE);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      g.scale(1 / SCALE, 1 / SCALE); // drawing below stays in page pixels
      font = '';
      drops = Array.from({ length: Math.ceil(width / CELL) }, (_, i) => drops[i] || newDrop(height, true));
    };
    size();
    window.addEventListener('resize', size);
    const off = ctx.onFrame(() => {
      const tokens = ctx.tokens();
      const wanted = `${CELL - 4}px ${tokens['--mono']}`;
      if (font !== wanted) { g.font = wanted; font = wanted; }
      g.globalCompositeOperation = 'destination-out'; // what was drawn before fades
      g.globalAlpha = 1;
      g.fillStyle = `rgba(0, 0, 0, ${FADE})`;
      g.fillRect(0, 0, width, height);
      g.globalCompositeOperation = 'source-over';
      g.fillStyle = tokens['--accent'];
      drops.forEach((d, i) => {
        if (!d.on) {
          if (Math.random() < 0.02) d.on = true;
          return;
        }
        d.y += CELL * d.speed;
        if (d.y > 0 && d.y < height) g.fillText(GLYPHS[(Math.random() * GLYPHS.length) | 0], i * CELL + 2, d.y);
        if (d.y > height + CELL) Object.assign(d, newDrop(height, false));
      });
    }, FPS);
    return () => {
      off();
      window.removeEventListener('resize', size);
      canvas.remove();
    };
  }
};
