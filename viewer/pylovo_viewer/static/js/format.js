// Number formatting in the browser's locale, with units.

const cache = new Map();
function fmt(opts) {
  const key = JSON.stringify(opts);
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(undefined, opts));
  return cache.get(key);
}
const missing = (v) => v === null || v === undefined || v === '' || Number.isNaN(Number(v));

export function num(value, digits = 0) {
  return missing(value) ? '–' : fmt({ maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value);
}
export function auto(value) {
  if (missing(value)) return '–';
  const abs = Math.abs(value);
  return fmt({ maximumFractionDigits: abs >= 100 ? 0 : abs >= 10 ? 1 : 2 }).format(value);
}
export function meters(value) {
  if (missing(value)) return '–';
  return value >= 1000 ? `${num(value / 1000, 2)} km` : `${num(value, value < 10 ? 1 : 0)} m`;
}
export function km(value) {
  return missing(value) ? '–' : value < 1 ? `${num(value * 1000, 0)} m` : `${num(value, value < 10 ? 2 : 1)} km`;
}
export function kw(value) {
  return missing(value) ? '–' : value >= 1000 ? `${num(value / 1000, 2)} MW` : `${num(value, value < 10 ? 1 : 0)} kW`;
}
export function mw(value) {
  return missing(value) ? '–' : `${num(value, value < 10 ? 2 : 1)} MW`;
}
export function kva(value) {
  return missing(value) ? '–' : value >= 1000 ? `${num(value / 1000, 2)} MVA` : `${num(value, 0)} kVA`;
}
export function pct(value, digits = 1) {
  return missing(value) ? '–' : `${num(value, digits)} %`;
}
export function pu(value) {
  return missing(value) ? '–' : `${num(value, 3)} p.u.`;
}
export function kv(value) {
  return missing(value) ? '–' : `${num(value, 0)} kV`;
}
export function count(n, one, many = `${one}s`) {
  return `${num(n)} ${Number(n) === 1 ? one : many}`;
}
export function date(text) {
  if (!text) return '';
  const d = new Date(text);
  return Number.isNaN(d.getTime()) ? String(text)
    : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
/** Any attribute value for the "All attributes" table. */
export function value(v) {
  if (v === null || v === undefined || v === '') return '–';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : auto(v);
  if (Array.isArray(v)) return v.length ? v.join(', ') : '–';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
