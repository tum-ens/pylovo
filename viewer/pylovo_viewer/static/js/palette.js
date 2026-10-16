// Colours of the map, light and dark, and colour schemes that give MapLibre expressions, legend
// entries and plain JS colours from one description (so map, legend and 3D roofs always agree).
//
// Categorical hues: the validated reference palette of the dataviz method (adjacent pairs pass
// colour-blind separation in both modes). Identity on the map (grids, supply areas, rings) uses
// neighbour-aware slots computed by the server, so neighbours never share a hue.

export const PALETTE = {
  light: {
    cat: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
    blue: ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'],
    res: ['#6da7ec', '#3987e5', '#1c5cab', '#0d366b'],        // ordinal, validated: SFH, TH, MFH, AB
    ord3: ['#6da7ec', '#2a78d6', '#184f95'],                  // ordinal, validated: rural, semi-urban, urban
    slate: ['#aab2c0', '#8791a3', '#667085', '#4a5366', '#313949', '#1b2130'],
    ink: { primary: '#121620', secondary: '#4a5160', muted: '#8a93a3', neutral: '#9aa3b2', faint: '#c3c8d2' },
    surface: '#ffffff', accent: '#4f46e5', building: '#b9c0cc',
    settlement: { rural: '#efe6d6', urban: '#e0cfb1' },
  },
  dark: {
    cat: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
    blue: ['#0d366b', '#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4', '#cde2fb'],
    res: ['#184f95', '#256abf', '#3987e5', '#86b6ef'],
    ord3: ['#184f95', '#3987e5', '#9ec5f4'],
    slate: ['#4f596b', '#6b7587', '#8b94a5', '#abb3c1', '#cbd1db', '#e8ecf2'],
    ink: { primary: '#eef0f4', secondary: '#c3c9d4', muted: '#7d8594', neutral: '#6b7383', faint: '#3a404c' },
    surface: '#14171e', accent: '#8b8cf8', building: '#4a5263',
    settlement: { rural: '#3a3427', urban: '#4a3f2c' },
  },
};
export const STATUS = { good: '#0ca30c', warning: '#fab219', serious: '#ec835a', critical: '#d03b3b' };
export const LOADING_STOPS = [[0, '#0ca30c'], [50, '#0ca30c'], [75, '#fab219'], [90, '#ec835a'], [100, '#d03b3b'], [130, '#7a1414']];
export const VOLTAGE_STOPS = [[0.88, '#7a1414'], [0.9, '#d03b3b'], [0.92, '#ec835a'], [0.945, '#fab219'], [0.975, '#0ca30c'], [1.03, '#0ca30c'], [1.05, '#fab219'], [1.1, '#d03b3b']];

export function theme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}
export function pal() {
  return PALETTE[theme()];
}

// Voltage levels of HV/MV context grids: the usual grid-map hues (380 kV red, 220 kV green, 110 kV
// violet; validated all-pairs, colour-blind pairs in the 6-8 band, so every level also has its own
// width step) and neutral inks below 110 kV.
export function voltageLevels() {
  const p = pal();
  return [
    ['380', p.cat[7], '380 kV (EHV)', 3.2], ['220', p.cat[5], '220 kV (EHV)', 2.6], ['110', p.cat[6], '110 kV (HV)', 2.0],
    ['mv', p.ink.secondary, 'MV (10–30 kV)', 1.3], ['lv', p.slate[1], 'LV', 0.8], ['unknown', p.ink.neutral, 'Voltage unknown', 0.9],
  ];
}
const matches = (v, value) => (Array.isArray(v) ? v.includes(value) : v === value);

// ---------------------------------------------------------------------------------- schemes
// { type: 'match', prop, entries: [[value, colour, label]], fallback: [colour, label] }
// { type: 'slot', prop }                      neighbour-aware slots 0..7
// { type: 'ramp', prop, stops: [[x, colour]], label, unit, fallback }
// { type: 'const', colour, label }

export const slot = (prop = 'slot') => ({ type: 'slot', prop });
export const constant = (colour, label) => ({ type: 'const', colour, label });

export function expr(scheme) {
  const p = pal();
  switch (scheme.type) {
    case 'const': return scheme.colour;
    case 'slot': return ['match', ['coalesce', ['get', scheme.prop], -1], ...p.cat.flatMap((c, i) => [i, c]), p.ink.neutral];
    case 'match': {
      const pairs = scheme.entries.flatMap(([value, colour]) => [value, colour]);
      const numeric = typeof [scheme.entries[0]?.[0]].flat()[0] === 'number';   // labels and input must share a type
      const input = numeric ? ['to-number', ['coalesce', ['get', scheme.prop], -9999]] : ['to-string', ['coalesce', ['get', scheme.prop], '']];
      return pairs.length ? ['match', input, ...pairs, scheme.fallback[0]] : scheme.fallback[0];
    }
    case 'ramp': return ['case', ['==', ['get', scheme.prop], null], scheme.fallback || p.ink.neutral,
      ['interpolate', ['linear'], ['to-number', ['get', scheme.prop]], ...scheme.stops.flat()]];
    default: return p.ink.neutral;
  }
}

function hexRgb(hex) {
  const v = parseInt(hex.slice(1), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}
function rgbHex(rgb) {
  return `#${rgb.map((x) => Math.round(x).toString(16).padStart(2, '0')).join('')}`;
}
export function interpolate(stops, value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  const v = Number(value);
  if (v <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) {
    const [x1, c1] = stops[i];
    const [x0, c0] = stops[i - 1];
    if (v <= x1) {
      const t = (v - x0) / (x1 - x0 || 1);
      const a = hexRgb(c0);
      const b = hexRgb(c1);
      return rgbHex(a.map((x, k) => x + (b[k] - x) * t));
    }
  }
  return stops[stops.length - 1][1];
}

/** The colour of one feature (its properties) under `scheme`, as the map draws it. */
export function colourOf(scheme, props) {
  const p = pal();
  const value = props?.[scheme.prop];
  switch (scheme.type) {
    case 'const': return scheme.colour;
    case 'slot': return value === null || value === undefined ? p.ink.neutral : p.cat[value] || p.ink.neutral;
    case 'match': return (scheme.entries.find(([v]) => matches(v, value)) || scheme.fallback)[1];
    case 'ramp': return interpolate(scheme.stops, value) || scheme.fallback || p.ink.neutral;
    default: return p.ink.neutral;
  }
}

export function gradient(stops) {
  const min = stops[0][0];
  const max = stops[stops.length - 1][0];
  return `linear-gradient(90deg, ${stops.map(([x, c]) => `${c} ${((x - min) / (max - min || 1)) * 100}%`).join(', ')})`;
}
