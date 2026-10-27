// Map symbols drawn on demand: transformer stations and the arrows at cable cabinets. A symbol
// layer names its image after the colours it needs, built from the layer's colour expression, so
// the symbol follows the colour mode. MapLibre asks for a missing image ('styleimagemissing') and
// addIcon draws it on a canvas.
const RATIO = 2;
const DARK = '#121620';
const LIGHT = '#ffffff';

const colourText = (fill) => ['to-string', ['to-color', fill]];
/** Image name of a station symbol: kind 'mvlv' or 'hvmv', `fill` a MapLibre colour expression. */
export const stationIcon = (kind, fill, surface) => ['concat', `station|${kind}|${surface}|`, colourText(fill)];
/** Image name of the arrow that marks the cable supplying a cabinet. */
export const arrowIcon = (fill, surface) => ['concat', `arrow|${surface}|`, colourText(fill)];

function rgb(text) {
  const m = String(text).match(/rgba?\(([^)]+)\)/);
  if (m) return m[1].split(',').slice(0, 3).map(Number);
  const v = parseInt(String(text).replace('#', ''), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}
function luminance(text) {
  const lin = (c) => (c / 255 <= 0.03928 ? c / 255 / 12.92 : ((c / 255 + 0.055) / 1.055) ** 2.4);
  const [r, g, b] = rgb(text).map(lin);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Colours of a station symbol on `fill`: the windings (dark or white, whichever reads better) and the edge. */
export function stationColours(fill, surface, kind = 'mvlv') {
  const l = luminance(fill);
  const ink = (1.05 / (l + 0.05) >= (l + 0.05) / (luminance(DARK) + 0.05)) ? LIGHT : DARK;
  // a light badge gets a dark edge, an HV/MV substation always an edge in its winding colour
  return { ink, edge: kind === 'hvmv' || ink === DARK ? ink : surface };
}

function draw(size, paint) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size * RATIO;
  const ctx = canvas.getContext('2d');
  ctx.scale(RATIO, RATIO);
  paint(ctx);
  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

// A rounded badge with the transformer symbol (two overlapping windings); HV/MV substations are
// larger and have an inner frame.
function drawStation(kind, surface, fill) {
  const hv = kind === 'hvmv';
  const size = hv ? 34 : 26;
  const { ink, edge } = stationColours(fill, surface, kind);
  return draw(size, (ctx) => {
    const pad = hv ? 2.5 : 2;
    ctx.beginPath();
    ctx.roundRect(pad, pad, size - 2 * pad, size - 2 * pad, hv ? 8 : 6);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.lineWidth = hv ? 2.4 : 1.8;
    ctx.strokeStyle = edge;
    ctx.stroke();
    ctx.strokeStyle = ink;
    if (hv) {
      ctx.beginPath();
      ctx.roundRect(6.5, 6.5, size - 13, size - 13, 4);
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }
    const r = hv ? 6 : 5.2;
    ctx.lineWidth = hv ? 2.2 : 1.9;
    for (const side of [-1, 1]) {
      ctx.beginPath();
      ctx.arc(size / 2 + side * 0.62 * r, size / 2, r, 0, Math.PI * 2);
      ctx.stroke();
    }
  });
}

// An arrowhead pointing up, its tip at the top edge (the layer anchors it there and rotates it).
function drawArrow(surface, fill) {
  const s = 12;
  return draw(s, (ctx) => {
    ctx.beginPath();
    ctx.moveTo(s / 2, 1.2);
    ctx.lineTo(s - 1.5, s - 1.5);
    ctx.lineTo(s / 2, s - 4.2);
    ctx.lineTo(1.5, s - 1.5);
    ctx.closePath();
    ctx.lineJoin = 'round';
    ctx.lineWidth = 1.8;
    ctx.strokeStyle = surface;
    ctx.stroke();
    ctx.fillStyle = fill;
    ctx.fill();
  });
}

const PAINT = { station: drawStation, arrow: drawArrow };

/** Add the image `name` (from stationIcon or arrowIcon) to the map; other names are left alone. */
export function addIcon(map, name) {
  const [type, ...args] = name.split('|');
  if (PAINT[type] && !map.hasImage(name)) map.addImage(name, PAINT[type](...args), { pixelRatio: RATIO });
}
