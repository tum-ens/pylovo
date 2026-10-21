// The MapLibre map: basemaps, the viewer layers in a fixed draw order, picking, selection
// highlight and 3D. It holds the layer data itself, so a basemap change (a new style) simply
// re-adds every source and layer.
import * as maplibregl from 'maplibre-gl';
import { mapLayers, STYLES } from './styles.js';
import { pal } from './palette.js';
import { createLod2Layer } from './lod2.js';
import { addIcon } from './icons.js';

const GLYPHS = 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf';
export const BASEMAPS = {
  light: { label: 'Light', style: 'https://tiles.openfreemap.org/styles/positron' },
  dark: { label: 'Dark', style: 'https://tiles.openfreemap.org/styles/dark' },
  streets: { label: 'Streets', style: 'https://tiles.openfreemap.org/styles/liberty' },
  osm: { label: 'OpenStreetMap', style: () => ({ version: 8, glyphs: GLYPHS,
    sources: { osm: { type: 'raster', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize: 256, maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' } },
    layers: [{ id: 'osm', type: 'raster', source: 'osm' }] }) },
  none: { label: 'No basemap', style: () => ({ version: 8, glyphs: GLYPHS, sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': pal().surface } }] }) },
};

// Draw order, bottom to top ('lod2' is the custom 3D layer).
export const Z_ORDER = ['postcodes', 'mv_areas', 'lv_areas', 'mv_customers', 'lv_buildings', 'lod2', 'hv_sites',
  'mv_osm_lines', 'hv_lines', 'lv_cables', 'mv_lines', 'hv_busbars', 'lv_cabinets', 'mv_open_points', 'mv_stations',
  'hv_substations'];
const HOVER = new Set(['postcodes', 'lv_areas', 'mv_areas']);
const EMPTY = { type: 'FeatureCollection', features: [] };
export const PITCH_3D = 25;              // camera tilt of the 3D view (65° above the ground)
const HIGHLIGHT = '#ffe600';             // a clicked line
const LINES = ['in', ['geometry-type'], ['literal', ['LineString', 'MultiLineString']]];

function labelPoints(fc) {
  return { type: 'FeatureCollection', features: fc.features.filter((f) => f.properties.lon !== undefined)
    .map((f) => ({ type: 'Feature', properties: f.properties, geometry: { type: 'Point', coordinates: [f.properties.lon, f.properties.lat] } })) };
}

// Web Mercator, for the direction of a line end as drawn on the map.
const mercator = ([lon, lat]) => [lon * Math.PI / 180, Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360))];

/** The end of every cable that supplies a cabinet (`to_cabinet`), with its direction (degrees from north). */
function cabinetArrows(fc) {
  const features = [];
  for (const f of fc.features) {
    if (!f.properties.to_cabinet || f.geometry?.type !== 'LineString') continue;
    const c = f.geometry.coordinates;
    const [x1, y1] = mercator(c[c.length - 1]);
    for (let i = c.length - 2; i >= 0; i--) {
      const [x0, y0] = mercator(c[i]);
      if (Math.hypot(x1 - x0, y1 - y0) < 1e-8) continue;   // skip zero-length segments at the end
      features.push({ type: 'Feature', properties: { ...f.properties, bearing: Math.atan2(x1 - x0, y1 - y0) * 180 / Math.PI },
        geometry: { type: 'Point', coordinates: c[c.length - 1] } });
      break;
    }
  }
  return { type: 'FeatureCollection', features };
}

/** The overhead parts of mixed MV lines (`overhead`: MultiLineString coordinates), with the line's properties. */
function overheadParts(fc) {
  return { type: 'FeatureCollection', features: fc.features.filter((f) => f.properties.overhead)
    .map((f) => ({ type: 'Feature', properties: { ...f.properties, overhead: null },
      geometry: { type: 'MultiLineString', coordinates: f.properties.overhead } })) };
}

// Sources derived from a layer's data, named `<layer>:<key>`, for the layers whose style sets the key.
const DERIVED = { labels: labelPoints, arrows: cabinetArrows, overhead: overheadParts };

export function boundsOf(geometry) {
  let w = Infinity; let s = Infinity; let e = -Infinity; let n = -Infinity;
  const walk = (c) => {
    if (typeof c[0] === 'number') { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); } else c.forEach(walk);
  };
  walk(geometry.coordinates);
  return Number.isFinite(w) ? [w, s, e, n] : null;
}

export class MapView {
  constructor(container, { basemap, view, d3, onMove, onHover, onClick, onReady }) {
    this.layers = {};        // viewer layer id -> { data, visible, mode, attribution, sublayers: [] }
    this.d3 = !!d3;
    this.lod2Hide = null;    // object ids drawn as LoD2 (their extrusions are hidden)
    this.selected = {};      // source -> fid with feature-state "selected"
    this.hovered = null;
    this.highlight = EMPTY;
    this.basemap = basemap;
    this.ready = false;
    this.handlers = { onMove, onHover, onClick };
    this.map = new maplibregl.Map({
      container, style: this.styleOf(basemap), center: view?.center || [10.45, 51.16], zoom: view?.zoom ?? 5.3,
      pitch: view ? view.pitch : (this.d3 ? PITCH_3D : 0), bearing: view?.bearing || 0, maxPitch: 75, attributionControl: false,
      canvasContextAttributes: { antialias: true },
    });
    this.map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
    this.map.addControl(new maplibregl.ScaleControl({ maxWidth: 110 }), 'bottom-right');
    this.map.on('style.load', () => {
      this.reloading = false;
      this.restore();
      if (!this.ready) { this.ready = true; onReady?.(); }
    });
    this.map.setMissingStyleImageResolver((id) => addIcon(this.map, id));
    this.map.on('moveend', () => this.handlers.onMove?.(this.view()));
    this.map.on('mousemove', (e) => this.hover(e));
    this.map.on('mouseout', () => { this.setHover(null); this.handlers.onHover?.(null); });
    this.map.on('click', (e) => this.click(e));
  }

  styleOf(key) {
    const style = (BASEMAPS[key] || BASEMAPS.light).style;
    return typeof style === 'function' ? style() : style;
  }

  /** Switch the basemap; true when a new style loads (its style.load re-adds every layer). */
  setBasemap(key) {
    if (key === this.basemap) return false;
    this.basemap = key;
    this.reloading = true;
    this.map.setStyle(this.styleOf(key), { diff: false });
    return true;
  }

  restore() {
    const map = this.map;
    map.addLayer(createLod2Layer('lod2'));
    for (const layer of Object.values(this.layers)) { layer.sublayers = []; layer.pickable = []; }   // the old style is gone
    for (const id of Object.keys(this.layers)) {
      this.ensureSource(id);
      if (this.layers[id].visible) this.addSublayers(id);
    }
    map.addSource('highlight', { type: 'geojson', data: this.highlight });
    const p = pal();
    map.addLayer({ id: 'highlight:fill', type: 'line', source: 'highlight', filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'line-color': p.accent, 'line-width': 3.5, 'line-opacity': 0.95 } });
    const width = (add) => ['interpolate', ['linear'], ['zoom'], 8, 4 + add, 14, 6 + add, 18, 9 + add];
    map.addLayer({ id: 'highlight:casing', type: 'line', source: 'highlight', filter: LINES,
      layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#121620', 'line-width': width(3), 'line-opacity': 0.6 } });
    map.addLayer({ id: 'highlight:line', type: 'line', source: 'highlight', filter: LINES,
      layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': HIGHLIGHT, 'line-width': width(0) } });
    map.addLayer({ id: 'highlight:point', type: 'circle', source: 'highlight', filter: ['all', ['==', ['geometry-type'], 'Point'], ['!', ['has', 'open']]],
      paint: { 'circle-radius': 13, 'circle-color': p.accent, 'circle-opacity': 0.18, 'circle-stroke-color': p.accent, 'circle-stroke-width': 2.5 } });
    // the normally-open points of a selected MV ring, on top of it at every zoom
    map.addLayer({ id: 'highlight:open', type: 'circle', source: 'highlight', filter: ['has', 'open'],
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 4.5, 14, 7, 18, 9], 'circle-color': '#ffffff',
        'circle-stroke-color': '#121620', 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 8, 2, 14, 3] } });
    this.applySelected();
  }

  ensureSource(id) {
    const map = this.map;
    const layer = this.layers[id];
    if (!map.getSource(id)) {
      map.addSource(id, { type: 'geojson', data: layer.data || EMPTY, promoteId: 'fid', tolerance: 0.25,
        ...(layer.attribution ? { attribution: layer.attribution } : {}) });
      for (const [key, derive] of Object.entries(DERIVED)) {
        if (STYLES[id]?.[key]) map.addSource(`${id}:${key}`, { type: 'geojson', data: derive(layer.data || EMPTY) });
      }
    }
  }

  beforeId(id) {
    const after = Z_ORDER.slice(Z_ORDER.indexOf(id) + 1);
    for (const next of after) {
      if (next === 'lod2') { if (this.map.getLayer('lod2')) return 'lod2'; continue; }
      const first = this.layers[next]?.sublayers?.[0];
      if (first && this.map.getLayer(first)) return first;
    }
    return this.map.getLayer('highlight:fill') ? 'highlight:fill' : undefined;
  }

  addSublayers(id) {
    const layer = this.layers[id];
    // a match finds the id by lookup, an 'in' would scan the list (up to 5,000 ids) for every building
    const hide = id === 'lv_buildings' && this.lod2Hide?.length ? ['match', ['get', 'fid'], this.lod2Hide, false, true] : null;
    const defs = mapLayers(id, layer.mode, this.d3, hide);
    const before = this.beforeId(id);
    for (const def of defs) {
      const { pick, ...spec } = def;
      if (layer.minZoom) spec.minzoom = Math.max(spec.minzoom || 0, layer.minZoom);
      this.map.addLayer(spec, before);
    }
    layer.sublayers = defs.map((d) => d.id);
    layer.pickable = defs.filter((d) => d.pick !== false).map((d) => d.id);
  }

  removeSublayers(id) {
    for (const sub of this.layers[id]?.sublayers || []) if (this.map.getLayer(sub)) this.map.removeLayer(sub);
    if (this.layers[id]) { this.layers[id].sublayers = []; this.layers[id].pickable = []; }
  }

  /** Show or hide a layer, or change its colour mode. */
  setLayer(id, { visible, mode, attribution, minZoom }) {
    const layer = this.layers[id] || (this.layers[id] = { data: null, visible: false, mode, sublayers: [], pickable: [], attribution, minZoom });
    const changed = (visible !== undefined && visible !== layer.visible) || (mode !== undefined && mode !== layer.mode);
    if (visible !== undefined) layer.visible = visible;
    if (mode !== undefined) layer.mode = mode;
    if (!this.map.isStyleLoaded() && !this.ready) return;
    this.ensureSource(id);
    if (changed) {
      this.removeSublayers(id);
      if (layer.visible) this.addSublayers(id);
    }
  }

  setData(id, fc) {
    const layer = this.layers[id] || (this.layers[id] = { visible: false, sublayers: [], pickable: [] });
    layer.data = fc;
    const source = this.ready ? this.map.getSource(id) : null;
    if (source) {
      source.setData(fc);
      for (const [key, derive] of Object.entries(DERIVED)) this.map.getSource(`${id}:${key}`)?.setData(derive(fc));
      this.applySelected();
    }
  }

  data(id) {
    return this.layers[id]?.data || null;
  }

  /** Re-add every visible layer (new theme colours); a style that is still loading does it itself. */
  restyle() {
    if (this.reloading) return;
    for (const id of Object.keys(this.layers)) {
      this.removeSublayers(id);
      if (this.layers[id].visible) this.addSublayers(id);
    }
    for (const id of ['highlight:fill', 'highlight:point']) {   // a clicked line stays yellow
      if (!this.map.getLayer(id)) continue;
      const prop = id.endsWith('point') ? 'circle-stroke-color' : 'line-color';
      this.map.setPaintProperty(id, prop, pal().accent);
      if (id.endsWith('point')) this.map.setPaintProperty(id, 'circle-color', pal().accent);
    }
    if (this.basemap === 'none') this.map.setPaintProperty('background', 'background-color', pal().surface);
  }

  set3D(on) {
    if (on === this.d3) return;
    this.d3 = on;
    this.map.easeTo({ pitch: on ? PITCH_3D : 0, duration: 700 });
    if (this.layers.lv_buildings?.visible) { this.removeSublayers('lv_buildings'); this.addSublayers('lv_buildings'); }
  }

  setLod2Hide(ids) {
    const same = (a, b) => (a?.length || 0) === (b?.length || 0) && (a || []).every((x, i) => x === b[i]);
    if (same(ids, this.lod2Hide)) return;   // re-adding the layer would rebuild all its tiles
    this.lod2Hide = ids;
    if (this.layers.lv_buildings?.visible && this.d3) { this.removeSublayers('lv_buildings'); this.addSublayers('lv_buildings'); }
  }

  // ----------------------------------------------------------------------------- selection
  applySelected() {
    for (const [source, fid] of Object.entries(this.selected)) {
      if (this.map.getSource(source)) this.map.setFeatureState({ source, id: fid }, { selected: true });
    }
  }

  /** Feature-state "selected" for one feature of a source (`fid` null clears the selection). */
  select(source, fid) {
    for (const [s, old] of Object.entries(this.selected)) {
      if (this.map.getSource(s)) this.map.removeFeatureState({ source: s, id: old }, 'selected');
    }
    this.selected = fid === null || fid === undefined ? {} : { [source]: fid };
    this.applySelected();
  }

  /** Highlight a feature (its geometry), with normally-open points (Point geometries) on top. */
  setHighlight(feature, openPoints = []) {
    this.highlight = feature ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: feature.geometry },
      ...openPoints.map((geometry) => ({ type: 'Feature', properties: { open: true }, geometry }))] } : EMPTY;
    this.map.getSource('highlight')?.setData(this.highlight);
  }

  /** The full feature of a layer by id (the rendered one is cut at tile borders). */
  feature(layerId, fid) {
    return this.layers[layerId]?.data?.features.find((f) => f.properties.fid === fid) || null;
  }

  // -------------------------------------------------------------------------------- picking
  pickable() {
    const out = [];
    for (const id of [...Z_ORDER].reverse()) {
      const layer = this.layers[id];
      if (layer?.visible) out.push(...(layer.pickable || []).filter((sub) => this.map.getLayer(sub)));
    }
    return out;
  }

  pick(point, pad = 4) {
    const layers = this.pickable();
    if (!layers.length) return [];
    const box = [[point.x - pad, point.y - pad], [point.x + pad, point.y + pad]];
    const seen = new Set();
    const hits = [];
    for (const f of this.map.queryRenderedFeatures(box, { layers })) {
      const key = `${f.source}:${f.properties.fid}`;
      if (seen.has(key)) continue;
      seen.add(key);
      hits.push({ layer: f.source, fid: f.properties.fid, props: f.properties });
    }
    return hits;
  }

  setHover(hit) {
    if (this.hovered && (!hit || hit.layer !== this.hovered.layer || hit.fid !== this.hovered.fid)) {
      if (this.map.getSource(this.hovered.layer)) this.map.setFeatureState({ source: this.hovered.layer, id: this.hovered.fid }, { hover: false });
      this.hovered = null;
    }
    if (hit && HOVER.has(hit.layer) && !this.hovered) {
      this.map.setFeatureState({ source: hit.layer, id: hit.fid }, { hover: true });
      this.hovered = { layer: hit.layer, fid: hit.fid };
    }
  }

  hover(e) {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      const hit = this.pick(e.point, 3)[0] || null;
      this.setHover(hit);
      this.map.getCanvas().style.cursor = hit ? 'pointer' : '';
      this.handlers.onHover?.(hit ? { ...hit, x: e.point.x, y: e.point.y } : null);
    });
  }

  click(e) {
    this.handlers.onClick?.(this.pick(e.point, 6), e.lngLat);
  }

  // ----------------------------------------------------------------------------------- view
  view() {
    const c = this.map.getCenter();
    return { center: [c.lng, c.lat], zoom: this.map.getZoom(), pitch: this.map.getPitch(), bearing: this.map.getBearing() };
  }

  /** [west, south, east, north] of the view, padded by `pad` (fraction) on every side. */
  bbox(pad = 0) {
    const b = this.map.getBounds();
    const dx = (b.getEast() - b.getWest()) * pad;
    const dy = (b.getNorth() - b.getSouth()) * pad;
    return [b.getWest() - dx, b.getSouth() - dy, b.getEast() + dx, b.getNorth() + dy];
  }

  fit(bounds, padding, maxZoom = 16) {
    if (!bounds) return;
    this.map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], { padding, maxZoom, duration: 800, pitch: this.map.getPitch() });
  }
}
