// Chart.js falls back to its BasicPlatform when there is no DOM, and that platform is
// happy with any object that answers getContext('2d'). That is enough to build real
// Chart instances under `node --test` — no jsdom, no native canvas — so the parts of
// charts.js that only a live chart exercises stay testable.

const noop = () => {};

/**
 * Canvas stub that records every fillText call.
 *
 * Returns `{ canvas, drawn }`; `drawn` accumulates `{ text, x, y }` for the text the
 * chart paints, which is how a test sees whether data labels were actually drawn.
 */
export function stubCanvas({ width = 400, height = 300 } = {}) {
  const drawn = [];
  const canvas = {
    width,
    height,
    clientWidth: width,
    clientHeight: height,
    style: {},
    getAttribute: () => null,
    setAttribute: noop,
    addEventListener: noop,
    removeEventListener: noop,
    getContext() {
      return {
        canvas: this,
        save: noop,
        restore: noop,
        beginPath: noop,
        closePath: noop,
        moveTo: noop,
        lineTo: noop,
        arc: noop,
        arcTo: noop,
        rect: noop,
        roundRect: noop,
        bezierCurveTo: noop,
        quadraticCurveTo: noop,
        ellipse: noop,
        fill: noop,
        stroke: noop,
        clip: noop,
        fillRect: noop,
        strokeRect: noop,
        clearRect: noop,
        translate: noop,
        rotate: noop,
        scale: noop,
        setTransform: noop,
        resetTransform: noop,
        setLineDash: noop,
        getLineDash: () => [],
        createLinearGradient: () => ({ addColorStop: noop }),
        createRadialGradient: () => ({ addColorStop: noop }),
        createPattern: () => null,
        drawImage: noop,
        putImageData: noop,
        // Chart.js reads .width off this to lay out the legend and the ticks.
        measureText: (text) => ({
          width: String(text).length * 6,
          actualBoundingBoxAscent: 8,
          actualBoundingBoxDescent: 2,
        }),
        fillText: (text, x, y) => drawn.push({ text: String(text), x, y }),
        strokeText: noop,
      };
    },
  };
  return { canvas, drawn };
}

/** i18n.js reads the stored locale on the first format call; there is no DOM here. */
export function stubLocaleStorage() {
  globalThis.localStorage = { getItem: () => null, setItem: noop };
}
