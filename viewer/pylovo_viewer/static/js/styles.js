// How each layer looks: its colour modes, its MapLibre layers, and the facts its tooltip and the
// inspector show. Colours come from schemes (palette.js), so the legend is derived from the same
// description as the map.
import { LOADING_STOPS, STATUS, VOLTAGE_STOPS, constant, expr, pal, slot, voltageLevels } from './palette.js';
import * as f from './format.js';

// Line width that grows with the zoom (exponentially, like the GridPlanner map).
export function lw(base, mult = null, add = 0) {
  return ['interpolate', ['exponential', 1.6], ['zoom'],
    ...[[9, 0.22], [12, 0.4], [15, 1], [19, 3.2]].flatMap(([z, k]) => {
      let out = base * k;
      if (mult) out = ['*', out, mult];
      if (add) out = ['+', out, add * Math.min(1, k)];
      return [z, out];
    })];
}
const radius = (r) => ['interpolate', ['exponential', 1.4], ['zoom'], 9, ['*', 0.45, r], 14, r, 18, ['*', 1.9, r]];
const parallelWidth = ['+', 1, ['*', 0.5, ['-', ['coalesce', ['get', 'parallel'], 1], 1]]];
const hover = (on, off) => ['case', ['boolean', ['feature-state', 'hover'], false], on, off];
// Zoom must be the top-level input of a MapLibre expression: per-zoom outputs carry the state.
const zoomed = (stops, fn) => ['interpolate', ['linear'], ['zoom'], ...stops.flatMap(([z, v]) => [z, fn(v)])];
const selected = (on, off) => ['case', ['boolean', ['feature-state', 'selected'], false], on, off];
const label = (text, size = 11) => ({
  'text-field': text, 'text-font': ['Noto Sans Regular'], 'text-size': size, 'text-optional': true,
});
const prettyCable = (t) => String(t || '').replace(/_(\d+)_(\d+)$/, ' $1×$2').replace(/_/g, ' ');

// --------------------------------------------------------------------------------- schemes
const STATUS_ENTRIES = () => [
  ['converged', STATUS.good, 'Converged'], ['voltage_violation', STATUS.critical, 'Voltage violation'],
  ['loading_violation', STATUS.serious, 'Overloaded'], ['not_converged', '#7a1414', 'Not converged'],
  ['failed', '#7a1414', 'Failed'], ['done', STATUS.good, 'Done'],
];
const status = (prop) => ({ type: 'match', prop, entries: STATUS_ENTRIES(), fallback: [pal().ink.neutral, 'No result'] });
// Thousands of fine stations would bury the few problems: converged ones stay neutral.
const quietStatus = (prop) => ({ ...status(prop), entries: status(prop).entries.map(([v, c, l]) =>
  (v === 'converged' ? [v, pal().ink.secondary, 'Converged (no violation)'] : [v, c, l])) });
const buildingTypes = () => {
  const p = pal();
  return { type: 'match', prop: 'type', entries: [
    ['SFH', p.res[0], 'Single-family house'], ['TH', p.res[1], 'Terraced house'],
    ['MFH', p.res[2], 'Multi-family house'], ['AB', p.res[3], 'Apartment block'],
    ['Commercial', p.cat[1], 'Commercial'], ['Public', p.cat[2], 'Public'],
  ], fallback: [p.ink.neutral, 'Other / unknown'] };
};
const feeders = () => {
  const p = pal();
  return { type: 'match', prop: 'feeder', entries: p.cat.slice(0, 7).map((c, i) => [i + 1, c, `Feeder ${i + 1}`]),
    fallback: [p.ink.neutral, 'Feeder 8 and more'] };
};
const kvaRamp = () => ({ type: 'ramp', prop: 'kva', label: 'Rated power', unit: 'kVA',
  stops: [[100, pal().blue[1]], [250, pal().blue[2]], [400, pal().blue[3]], [630, pal().blue[4]], [1000, pal().blue[6]]] });
const loading = (prop, title) => ({ type: 'ramp', prop, label: title, unit: '%', stops: LOADING_STOPS });
const voltageScheme = (prop, numeric) => {
  const levels = voltageLevels();
  const entries = numeric
    ? levels.filter(([k]) => ['380', '220', '110'].includes(k)).map(([k, c, l]) => [Number(k), c, l])
    : levels.map(([k, c, l]) => [k, c, l]);
  return { type: 'match', prop, entries, fallback: [pal().ink.neutral, 'Other'] };
};
const voltageWidth = (prop, numeric) => ['match', ['get', prop],
  ...voltageLevels().filter(([k]) => !numeric || ['380', '220', '110'].includes(k))
    .flatMap(([k, , , w]) => [numeric ? Number(k) : k, w]), 1];

// ----------------------------------------------------------------------------------- styles
// layers(ctx): MapLibre layers of one viewer layer. ctx = { id, source, colour (expression of the
// current mode), scheme, d3 (3D on), p (palette) }. facts(props): title, subtitle and rows.
export const STYLES = {
  postcodes: {
    modes: { plain: { label: 'Outline', scheme: () => constant(pal().accent, 'Postcode area') },
      settlement: { label: 'Settlement type', scheme: () => ({ type: 'match', prop: 'settlement', entries: [
        [1, pal().ord3[0], 'Rural'], [2, pal().ord3[1], 'Semi-urban'], [3, pal().ord3[2], 'Urban']],
        fallback: [pal().ink.neutral, 'Unknown'] }) } },
    labels: true,
    layers: ({ id, colour, mode, p }) => [
      { id, type: 'fill', paint: { 'fill-color': colour,
        'fill-opacity': mode === 'settlement' ? zoomed([[9, 0.34], [14, 0.07]], (v) => hover(Math.min(v + 0.1, 0.45), v))
          : hover(0.14, selected(0.05, 0.012)) } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': mode === 'settlement' ? p.ink.secondary : p.accent,
        'line-width': selected(3, hover(2, 1.1)), 'line-opacity': 0.85 } },
      { id: `${id}:label`, type: 'symbol', source: `${id}:labels`, pick: false, minzoom: 8,
        layout: label(['step', ['zoom'], ['to-string', ['get', 'plz']], 11, ['coalesce', ['get', 'note'], ['to-string', ['get', 'plz']]]], 12),
        paint: { 'text-color': p.ink.primary, 'text-halo-color': p.surface, 'text-halo-width': 1.6,
          'text-opacity': ['interpolate', ['linear'], ['zoom'], 14, 1, 15.5, 0] } },
    ],
    facts: (o) => ({ title: `Postcode ${o.plz}`, subtitle: o.note, rows: [
      ['Grids', f.num(o.grids)], ['Settlement type', { 1: 'rural', 2: 'semi-urban', 3: 'urban' }[o.settlement] || '–']] }),
  },
  lv_areas: {
    modes: { grid: { label: 'Grid', scheme: () => slot() }, status: { label: 'Power flow', scheme: () => status('status') },
      kva: { label: 'Station size', scheme: kvaRamp } },
    layers: ({ id, colour }) => [
      { id, type: 'fill', paint: { 'fill-color': colour,
        'fill-opacity': zoomed([[12, 0.2], [15, 0.07], [17, 0.03]], (v) => hover(0.34, selected(Math.max(v, 0.12), v))) } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': selected(pal().accent, colour), 'line-width': selected(3.5, 1),
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 11, 0.5, 15, 0.9] } },
    ],
    facts: (o) => ({ title: `LV grid ${o.fid}`, subtitle: `PLZ ${o.plz} · cluster ${o.kcid}/${o.bcid}`, rows: [
      ['Station', f.kva(o.kva)], ['Buildings', f.num(o.buildings)], ['Power flow', statusText(o.status)]] }),
  },
  lv_stations: {
    modes: { status: { label: 'Power flow', scheme: () => quietStatus('status') }, grid: { label: 'Grid', scheme: () => slot() },
      origin: { label: 'Origin', scheme: () => ({ type: 'match', prop: 'origin', entries: [
        ['new', pal().cat[0], 'New (greenfield)'], ['osm', pal().cat[1], 'Existing (OSM)'], ['lod2', pal().cat[2], 'Existing (LoD2)']],
        fallback: [pal().ink.neutral, 'Other'] }) } },
    layers: ({ id, colour, p }) => [
      { id, type: 'circle', paint: {
        'circle-radius': ['interpolate', ['exponential', 1.4], ['zoom'], ...[[9, 0.3], [13, 0.65], [16, 1], [19, 1.7]].flatMap(([z, k]) =>
          [z, ['*', k, ['interpolate', ['linear'], ['coalesce', ['get', 'kva'], 250], 100, 3.6, 400, 5.2, 630, 6.2, 1260, 8.5]]])],
        'circle-color': colour, 'circle-stroke-color': p.surface, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 10, 0.8, 15, 2] } },
      { id: `${id}:label`, type: 'symbol', pick: false, minzoom: 15,
        layout: { ...label(['concat', ['to-string', ['get', 'kva']], ' kVA'], 11), 'text-offset': [0, 1.3], 'text-anchor': 'top' },
        paint: { 'text-color': p.ink.primary, 'text-halo-color': p.surface, 'text-halo-width': 1.6 } },
    ],
    facts: (o) => ({ title: `Substation of grid ${o.fid}`, subtitle: `PLZ ${o.plz} · cluster ${o.kcid}/${o.bcid}`, rows: [
      ['Rated power', f.kva(o.kva)], ['Power flow', statusText(o.status)],
      ['Origin', { new: 'new (greenfield)', osm: 'existing (OSM)', lod2: 'existing (LoD2)' }[o.origin] || '–']] }),
  },
  lv_cables: {
    modes: { type: { label: 'Cable type', scheme: () => ({ type: 'ramp', prop: 'mm2', label: 'Cross-section', unit: 'mm²',
      stops: [[50, pal().slate[0]], [95, pal().slate[1]], [150, pal().slate[2]], [185, pal().slate[3]], [240, pal().slate[4]], [300, pal().slate[5]]] }) },
    feeder: { label: 'Feeder', scheme: feeders }, grid: { label: 'Grid', scheme: () => slot() },
    role: { label: 'Feeder / service', scheme: () => ({ type: 'match', prop: 'role', entries: [
      ['feeder', pal().cat[0], 'Feeder cable'], ['service', pal().cat[1], 'Service cable']], fallback: [pal().ink.neutral, 'Other'] }) } },
    layers: ({ id, colour, mode, p }) => [
      { id: `${id}:service`, type: 'line', filter: ['==', ['get', 'role'], 'service'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': mode === 'type' ? p.ink.muted : colour, 'line-width': lw(1.0), 'line-opacity': 0.85 } },
      { id: `${id}:casing`, type: 'line', pick: false, filter: ['!=', ['get', 'role'], 'service'],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': p.surface, 'line-width': lw(2.6, parallelWidth, 2.4), 'line-opacity': 0.9 } },
      { id, type: 'line', filter: ['!=', ['get', 'role'], 'service'], layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': colour, 'line-width': lw(2.6, parallelWidth) } },
    ],
    extraLegend: (mode) => (mode === 'type' ? [{ kind: 'line', colour: pal().ink.muted, label: 'Service cable', thin: true }] : []),
    facts: (o) => ({ title: prettyCable(o.std_type), subtitle: o.role === 'service' ? 'Service cable' : 'Feeder cable', rows: [
      ['Length', f.meters(o.length_m)], ['Parallel', o.parallel > 1 ? `${o.parallel}×` : '1'],
      ['Feeder', o.feeder ? `#${o.feeder}` : '–'], ['Grid', o.grid]] }),
  },
  lv_cabinets: {
    layers: ({ id, p }) => [
      { id, type: 'circle', paint: { 'circle-radius': radius(3.2), 'circle-color': p.surface,
        'circle-stroke-color': p.ink.primary, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 14, 1.2, 18, 2.4] } },
      { id: `${id}:label`, type: 'symbol', pick: false, minzoom: 17,
        layout: { ...label(['to-string', ['get', 'outgoing']], 10), 'text-offset': [0, 1.2], 'text-anchor': 'top' },
        paint: { 'text-color': p.ink.secondary, 'text-halo-color': p.surface, 'text-halo-width': 1.4 } },
    ],
    legend: () => [{ kind: 'circle', hollow: true, colour: pal().ink.primary, label: 'Cable cabinet (label: outgoing cables)' }],
    facts: (o) => ({ title: 'Cable cabinet', subtitle: `grid ${o.grid}`, rows: [['Outgoing cables', f.num(o.outgoing)]] }),
  },
  lv_buildings: {
    modes: { grid: { label: 'Grid', scheme: () => slot() }, type: { label: 'Building type', scheme: buildingTypes },
      feeder: { label: 'Feeder', scheme: feeders },
      load: { label: 'Peak load', scheme: () => ({ type: 'ramp', prop: 'peak_kw', label: 'Peak load', unit: 'kW',
        stops: [[0, pal().blue[0]], [15, pal().blue[2]], [40, pal().blue[4]], [150, pal().blue[6]]] }) } },
    extrude: true,
    layers: ({ id, colour, d3, p, hide }) => (d3 ? [
      // its own id: MapLibre keeps a layer's render buckets per id, so a type change needs a new one
      { id: `${id}:3d`, type: 'fill-extrusion', ...(hide ? { filter: hide } : {}), paint: { 'fill-extrusion-color': colour,
        'fill-extrusion-height': ['coalesce', ['get', 'height'], 6], 'fill-extrusion-base': 0, 'fill-extrusion-opacity': 0.92 } },
    ] : [
      { id, type: 'fill', paint: { 'fill-color': colour, 'fill-opacity': theme3(p, 0.55, 0.62) } },
      { id: `${id}:line`, type: 'line', pick: false, minzoom: 16, paint: { 'line-color': colour, 'line-width': 0.8, 'line-opacity': 0.9 } },
    ]),
    facts: (o) => ({ title: o.use === 'Residential' ? (buildingTypes().entries.find(([v]) => v === o.type)?.[2] || o.type || 'Building') : (o.use || 'Building'),
      subtitle: o.fid, rows: [['Households', f.num(o.households)], ['Peak load', f.kw(o.peak_kw)],
        ['Height', o.height ? `${f.num(o.height, 1)} m · ${f.num(o.floors)} floors` : '–'], ['Feeder', o.feeder ? `#${o.feeder}` : '–'],
        ['Grid', o.grid], ...(o.mv_direct ? [['Connection', 'directly to MV']] : [])] }),
  },
  in_buildings: {
    modes: { type: { label: 'Building type', scheme: buildingTypes } },
    layers: ({ id, colour, p }) => [
      { id, type: 'fill', paint: { 'fill-color': colour, 'fill-opacity': theme3(p, 0.45, 0.5) } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': colour, 'line-width': 0.7 } },
    ],
    facts: (o) => ({ title: o.type || 'Building', subtitle: o.objectid, rows: [['Use', o.use || '–'],
      ['Households', f.num(o.households)], ['Height', o.height ? `${f.num(o.height, 1)} m · ${f.num(o.floors)} floors` : '–']] }),
  },
  in_transformers: {
    modes: { source: { label: 'Source', scheme: () => ({ type: 'match', prop: 'source', entries: [
      ['osm', pal().cat[0], 'OpenStreetMap'], ['dso', pal().cat[1], 'DSO data'], ['manual', pal().cat[2], 'Manual'],
      ['lod2', pal().ink.secondary, 'LoD2 building']], fallback: [pal().ink.neutral, 'Other'] }) } },
    layers: ({ id, colour, p }) => [
      { id, type: 'circle', paint: { 'circle-radius': radius(4.2), 'circle-color': colour,
        'circle-stroke-color': p.surface, 'circle-stroke-width': 1.5 } },
    ],
    facts: (o) => ({ title: 'Transformer candidate', subtitle: o.fid, rows: [['Source', o.source],
      ['Rated power', o.kva ? f.kva(o.kva) : 'unknown']] }),
  },
  in_ways: {
    layers: ({ id, p }) => [{ id, type: 'line', paint: { 'line-color': p.ink.muted, 'line-width': lw(1.6), 'line-opacity': 0.8 } }],
    legend: () => [{ kind: 'line', colour: pal().ink.muted, label: 'Street (routing graph)' }],
    facts: (o) => ({ title: o.klasse || 'Street', subtitle: o.objektart, rows: [['Length', f.meters(o.length_m)]] }),
  },
  in_connections: {
    layers: ({ id, p }) => [{ id, type: 'line', paint: { 'line-color': p.ink.neutral, 'line-width': lw(1.0), 'line-dasharray': [2, 1.5] } }],
    legend: () => [{ kind: 'line', dash: true, colour: pal().ink.neutral, label: 'House connection line' }],
    facts: (o) => ({ title: 'House connection line', rows: [['Length', f.meters(o.length_m)]] }),
  },
  lv_classes: {
    modes: Object.fromEntries(['kmeans', 'kmedoid', 'gmm'].map((m) => [m, { label: { kmeans: 'k-means', kmedoid: 'k-medoids', gmm: 'GMM' }[m],
      scheme: () => ({ type: 'match', prop: m, entries: pal().cat.map((c, i) => [i, c, `Cluster ${i}`]), fallback: [pal().ink.neutral, 'Other'] }) }])),
    layers: ({ id, colour, mode, p }) => [
      { id, type: 'circle', paint: { 'circle-radius': radius(6), 'circle-color': colour,
        'circle-stroke-color': ['case', ['coalesce', ['get', `${mode}_rep`], false], p.ink.primary, p.surface],
        'circle-stroke-width': ['case', ['coalesce', ['get', `${mode}_rep`], false], 3, 1.2] } },
    ],
    extraLegend: () => [{ kind: 'circle', hollow: true, colour: pal().ink.primary, label: 'Representative grid' }],
    facts: (o) => ({ title: `Grid ${o.fid}`, subtitle: `classification ${o.classification_id}`, rows: [
      ['k-means', o.kmeans], ['k-medoids', o.kmedoid], ['GMM', o.gmm]] }),
  },
  mv_areas: {
    modes: { area: { label: 'Supply area', scheme: () => slot() },
      voltage: { label: 'Voltage', scheme: () => ({ type: 'match', prop: 'vn_kv', entries: [[10, pal().cat[1], '10 kV'], [20, pal().cat[0], '20 kV']],
        fallback: [pal().ink.neutral, 'Other'] }) }, status: { label: 'Power flow', scheme: () => status('pf') } },
    layers: ({ id, colour, p }) => [
      { id, type: 'fill', paint: { 'fill-color': colour,
        'fill-opacity': zoomed([[8, 0.13], [11, 0.07], [14, 0.025]], (v) => hover(0.2, v)) } },
      { id: `${id}:line`, type: 'line', pick: false, filter: ['!=', ['get', 'complete'], false],
        paint: { 'line-color': selected(p.accent, p.ink.secondary), 'line-width': selected(3.5, 1.2), 'line-opacity': 0.85 } },
      { id: `${id}:open`, type: 'line', pick: false, filter: ['==', ['get', 'complete'], false],
        paint: { 'line-color': selected(p.accent, p.ink.secondary), 'line-width': selected(3.5, 1.2), 'line-opacity': 0.85, 'line-dasharray': [3, 2] } },
    ],
    extraLegend: () => [{ kind: 'line', dash: true, colour: pal().ink.secondary, label: 'Incomplete (cut by the region border)' }],
    facts: (o) => ({ title: `Supply area ${o.fid}`, subtitle: o.substation || 'HV/MV substation', rows: [
      ['Voltage', f.kv(o.vn_kv)], ['MV/LV stations', f.num(o.stations)], ['MV customers', f.num(o.customers)],
      ['Rings', f.num(o.rings)], ['Peak load', f.mw(o.peak_mw)], ['Max loading', f.pct(o.max_loading)],
      ['Max voltage drop', f.pct(o.max_drop, 2)], ['Power flow', statusText(o.pf)], ['Complete', o.complete ? 'yes' : 'no (border)']] }),
  },
  mv_sites: {
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': p.ink.primary, 'fill-opacity': 0.1 } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': p.ink.primary, 'line-width': 1.4 } },
    ],
    legend: () => [{ kind: 'fill', colour: pal().ink.primary, label: 'Substation site' }],
    facts: (o) => ({ title: o.name || 'Substation site', subtitle: o.source, rows: [] }),
  },
  mv_lines: {
    modes: { ring: { label: 'Ring', scheme: () => slot('ring_slot') },
      kind: { label: 'Cable / overhead', scheme: () => ({ type: 'match', prop: 'kind', entries: [
        ['cable', pal().cat[0], 'Cable'], ['overhead', pal().cat[1], 'Overhead line (dashed)'], ['mixed', pal().cat[6], 'Mixed']],
        fallback: [pal().ink.neutral, 'Other'] }) },
      role: { label: 'Role', scheme: () => ({ type: 'match', prop: 'role', entries: [
        ['ring', pal().cat[0], 'Open ring'], ['stub', pal().cat[2], 'Stub (T-off)'], ['dedicated', pal().cat[1], 'Dedicated feeder'],
        ['generator', pal().ink.secondary, 'Generator connection']], fallback: [pal().ink.neutral, 'Other'] }) },
      loading: { label: 'Loading (load case)', scheme: () => loading('loading', 'Line loading, load case') },
      loading_n1: { label: 'Loading (n-1)', scheme: () => loading('loading_n1', 'Line loading, n-1 case') },
      area: { label: 'Supply area', scheme: () => slot() } },
    layers: ({ id, colour, p }) => {
      const width = (add) => ['interpolate', ['exponential', 1.5], ['zoom'],
        ...[[8, 0.5], [12, 1.6], [16, 4]].flatMap(([z, k]) => [z, ['+', ['*', k, parallelWidth], add]])];
      const w = width(0);
      const notTie = ['!=', ['get', 'role'], 'tie'];
      return [
        { id: `${id}:tie`, type: 'line', filter: ['==', ['get', 'role'], 'tie'],
          paint: { 'line-color': p.ink.muted, 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 1, 14, 2.5], 'line-dasharray': [0.6, 1.6] } },
        { id: `${id}:casing`, type: 'line', pick: false, filter: notTie, layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': p.surface, 'line-width': width(1.6), 'line-opacity': 0.8 } },
        { id, type: 'line', filter: ['all', notTie, ['!=', ['get', 'kind'], 'overhead']], layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': colour, 'line-width': w } },
        { id: `${id}:overhead`, type: 'line', filter: ['all', notTie, ['==', ['get', 'kind'], 'overhead']],
          paint: { 'line-color': colour, 'line-width': w, 'line-dasharray': [2.4, 1.2] } },
      ];
    },
    extraLegend: (mode) => [
      ...(mode !== 'kind' ? [{ kind: 'line', dash: true, colour: pal().ink.secondary, label: 'Overhead line (dashed)' }] : []),
      { kind: 'line', dot: true, colour: pal().ink.muted, label: 'Tie between supply areas (normally open)' }],
    facts: (o) => ({ title: `MV ${o.kind === 'overhead' ? 'overhead line' : 'cable'}`, subtitle: `${o.role}${o.open ? ' · normally open' : ''}`, rows: [
      ['Type', o.std_type || '–'], ['Length', f.km(o.length_km)], ['Parallel', o.parallel > 1 ? `${o.parallel}×` : '1'],
      ['Ring / feeder', o.ring === null || o.ring === undefined ? '–' : `${o.ring} / ${o.feeder ?? '–'}`],
      ['Loading (load case)', f.pct(o.loading)], ['Loading (n-1)', f.pct(o.loading_n1)],
      ...(o.loading_feed_in !== null && o.loading_feed_in !== undefined ? [['Loading (feed-in)', f.pct(o.loading_feed_in)]] : []),
      ['Supply area', o.area]] }),
  },
  mv_open_points: {
    layers: ({ id, p }) => [
      { id, type: 'circle', paint: { 'circle-radius': radius(4.4), 'circle-color': p.surface,
        'circle-stroke-color': p.ink.primary, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 11, 1.5, 16, 2.6] } },
    ],
    legend: () => [{ kind: 'circle', hollow: true, colour: pal().ink.primary, label: 'Normally-open point' }],
    facts: (o) => ({ title: 'Normally-open point', subtitle: `ring ${o.ring} · supply area ${o.area}`, rows: [['Opens at node', o.node]] }),
  },
  mv_nodes: {
    modes: { kind: { label: 'Kind', scheme: () => ({ type: 'match', prop: 'kind', entries: [
      ['busbar', pal().ink.primary, 'HV/MV busbar'], ['station', pal().cat[0], 'MV/LV station'], ['customer', pal().cat[1], 'MV customer'],
      ['generator', pal().cat[2], 'Generator'], ['joint', pal().ink.neutral, 'Joint']], fallback: [pal().ink.neutral, 'Other'] }) },
    lv: { label: 'LV grid result', scheme: () => ({ type: 'match', prop: 'lv_status', entries: [
      ['voltage_violation', STATUS.critical, 'LV voltage violation'], ['not_converged', '#7a1414', 'LV not converged']],
      fallback: [STATUS.good, 'LV grid fine / not an LV station'] }) },
    ring: { label: 'Ring', scheme: () => slot('ring_slot') },
    voltage: { label: 'Voltage (load case)', scheme: () => ({ type: 'ramp', prop: 'vm', label: 'MV voltage, load case', unit: 'p.u.', stops: VOLTAGE_STOPS }) } },
    layers: ({ id, colour, p }) => [
      { id, type: 'circle', paint: {
        'circle-radius': radius(['match', ['get', 'kind'], 'busbar', 6.5, 'customer', 4.4, 'generator', 4.2, 'joint', 1.8, 3.4]),
        'circle-color': colour, 'circle-stroke-color': p.surface,
        'circle-stroke-width': zoomed([[10, 0.6], [15, 1.6]], (v) => ['match', ['get', 'kind'], 'joint', 0, v]) } },
    ],
    facts: (o) => ({ title: { busbar: 'HV/MV busbar', station: 'MV/LV station', customer: 'MV customer', generator: 'Generator', joint: 'Joint' }[o.kind] || o.kind,
      subtitle: `supply area ${o.area} · ${o.connection || '–'}`, rows: [
        ...(o.kva ? [['Rated power', f.kva(o.kva)]] : []), ...(o.households ? [['Households', f.num(o.households)]] : []),
        ...(o.technology ? [['Technology', o.technology]] : []), ...(o.grid ? [['LV grid', o.grid]] : []),
        ...(o.objectid ? [['Building', o.objectid]] : []),
        ['Ring / feeder', o.ring === null || o.ring === undefined ? '–' : `${o.ring} / ${o.feeder ?? '–'}`],
        ['Voltage (load)', f.pu(o.vm)], ...(o.lv_vm ? [['LV busbar voltage', f.pu(o.lv_vm)]] : []),
        ...(o.flags ? [['Flags', o.flags]] : [])] }),
  },
  mv_substations: {
    layers: ({ id, p }) => [
      { id, type: 'circle', paint: { 'circle-radius': radius(['case', ['get', 'used'], 8.5, 6.5]),
        'circle-color': ['case', ['get', 'used'], p.ink.primary, p.surface],
        'circle-stroke-color': ['case', ['get', 'used'], p.surface, p.ink.primary], 'circle-stroke-width': 2.4 } },
      { id: `${id}:label`, type: 'symbol', pick: false, minzoom: 10,
        layout: { ...label(['coalesce', ['get', 'name'], ''], 11.5), 'text-offset': [0, 1.5], 'text-anchor': 'top' },
        paint: { 'text-color': p.ink.primary, 'text-halo-color': p.surface, 'text-halo-width': 1.8 } },
    ],
    legend: () => [{ kind: 'circle', colour: pal().ink.primary, label: 'HV/MV substation (supplies an area)' },
      { kind: 'circle', hollow: true, colour: pal().ink.primary, label: 'Candidate, not used' }],
    facts: (o) => ({ title: o.name || 'HV/MV substation', subtitle: `${o.source}${o.used ? '' : ' · not used'}`, rows: [
      ['Transformers', o.transformers ? `${o.transformers} × ${f.num(o.mva / Math.max(o.transformers, 1), 0)} MVA` : '–'],
      ['Loading (n-0 / n-1)', `${f.pct(o.loading)} / ${f.pct(o.loading_n1)}`], ['Voltage tag', o.voltage || '–'],
      ['Operator', o.operator || '–'], ...(o.flags ? [['Flags', o.flags]] : [])] }),
  },
  mv_customers: {
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': p.cat[1], 'fill-opacity': 0.5 } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': p.cat[1], 'line-width': 1.2 } },
    ],
    legend: () => [{ kind: 'fill', colour: pal().cat[1], label: 'Building supplied from MV' }],
    facts: (o) => ({ title: 'MV customer building', subtitle: o.fid, rows: [['Use', o.nonres_use || o.use || '–'],
      ['Peak load', f.kw(o.peak_kw)], ['Households', f.num(o.households)]] }),
  },
  mv_settlements: {
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': ['case', ['get', 'urban'], p.settlement.urban, p.settlement.rural], 'fill-opacity': 0.7 } },
    ],
    legend: () => [{ kind: 'fill', colour: pal().settlement.urban, label: 'Urban settlement' },
      { kind: 'fill', colour: pal().settlement.rural, label: 'Rural settlement' }],
    facts: (o) => ({ title: `${o.urban ? 'Urban' : 'Rural'} settlement`, rows: [['Buildings', f.num(o.buildings)], ['Street length', f.km(o.km)]] }),
  },
  mv_graph: {
    layers: ({ id, p }) => [{ id, type: 'line', paint: { 'line-width': lw(1.4),
      'line-color': ['case', ['!', ['coalesce', ['get', 'in_settlement'], false]], p.ink.muted, ['get', 'urban'], p.cat[1], p.cat[3]] } }],
    legend: () => [{ kind: 'line', colour: pal().cat[1], label: 'Edge in urban settlement' },
      { kind: 'line', colour: pal().cat[3], label: 'Edge in rural settlement' }, { kind: 'line', colour: pal().ink.muted, label: 'Open field' }],
    facts: (o) => ({ title: o.klasse || 'Street edge', subtitle: o.in_settlement ? (o.urban ? 'urban' : 'rural') : 'open field',
      rows: [['Length', f.meters(o.length_m)]] }),
  },
  ctx_etrago_lines: {
    modes: { voltage: { label: 'Voltage', scheme: () => voltageScheme('v_nom', true) } },
    layers: ({ id, colour, p }) => [
      { id: `${id}:casing`, type: 'line', pick: false, paint: { 'line-color': p.surface, 'line-opacity': 0.6,
        'line-width': ['interpolate', ['linear'], ['zoom'], 5, 0.8, 10, ['+', 1.6, voltageWidth('v_nom', true)]] } },
      { id, type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': colour,
        'line-width': ['interpolate', ['linear'], ['zoom'], 5, ['*', 0.35, voltageWidth('v_nom', true)], 10, voltageWidth('v_nom', true), 15, ['*', 1.8, voltageWidth('v_nom', true)]] } },
    ],
    facts: (o) => ({ title: `eTraGo ${f.kv(o.v_nom)} line`, subtitle: `line ${o.fid} · bus ${o.bus0} → ${o.bus1}`, rows: [
      ['Length', f.km(o.length_km)], ['Capacity', `${f.num(o.s_nom)} MVA`], ['Cables', f.num(o.cables)]] }),
  },
  ctx_etrago_buses: {
    modes: { voltage: { label: 'Voltage', scheme: () => voltageScheme('v_nom', true) } },
    layers: ({ id, colour, p }) => [{ id, type: 'circle', paint: { 'circle-radius': radius(2.6), 'circle-color': colour,
      'circle-stroke-color': p.surface, 'circle-stroke-width': 0.8 } }],
    facts: (o) => ({ title: `eTraGo bus ${o.fid}`, subtitle: f.kv(o.v_nom), rows: [] }),
  },
  ctx_egon_districts: {
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': p.cat[6], 'fill-opacity': hover(0.12, 0) } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': p.cat[6], 'line-width': 1, 'line-opacity': 0.7, 'line-dasharray': [3, 2] } },
    ],
    legend: () => [{ kind: 'line', dash: true, colour: pal().cat[6], label: 'eGon MV grid district' }],
    facts: (o) => ({ title: `eGon MV grid district ${o.fid}`, rows: [] }),
  },
  ctx_egon_substations: {
    layers: ({ id, p }) => [{ id, type: 'circle', paint: { 'circle-radius': radius(3.6), 'circle-color': p.cat[6],
      'circle-stroke-color': p.surface, 'circle-stroke-width': 1.2 } }],
    legend: () => [{ kind: 'circle', colour: pal().cat[6], label: 'eGon HV/MV substation' }],
    facts: (o) => ({ title: o.name || 'eGon HV/MV substation', subtitle: `bus ${o.fid}`, rows: [['Voltage tag', o.voltage || '–'], ['Operator', o.operator || '–']] }),
  },
  ctx_osm_lines: {
    modes: { voltage: { label: 'Voltage', scheme: () => voltageScheme('level', false) } },
    layers: ({ id, colour }) => [{ id, type: 'line', layout: { 'line-cap': 'round' }, paint: { 'line-color': colour,
      'line-width': ['interpolate', ['linear'], ['zoom'], 7, ['*', 0.4, voltageWidth('level', false)], 12, voltageWidth('level', false), 16, ['*', 1.8, voltageWidth('level', false)]] } }],
    facts: (o) => ({ title: o.name || `OSM power ${o.subtype}`, subtitle: o.operator, rows: [['Voltage', o.voltage || '–'], ['Circuits', o.circuits || '–'], ['OSM way', o.fid]] }),
  },
  ctx_osm_substations: {
    modes: { voltage: { label: 'Voltage', scheme: () => voltageScheme('level', false) } },
    layers: ({ id, colour }) => [
      { id, type: 'fill', paint: { 'fill-color': colour, 'fill-opacity': 0.25 } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': colour, 'line-width': 1.2 } },
    ],
    facts: (o) => ({ title: o.name || 'OSM substation', subtitle: o.kind || o.operator, rows: [['Voltage', o.voltage || '–'], ['Operator', o.operator || '–']] }),
  },
  ctx_mastr: {
    modes: { technology: { label: 'Technology', scheme: () => ({ type: 'match', prop: 'technology', entries: [
      ['solar', pal().cat[1], 'Solar'], ['wind', pal().cat[0], 'Wind'], ['biomass', pal().cat[2], 'Biomass']],
      fallback: [pal().ink.neutral, 'Other (hydro, storage, combustion, ...)'] }) },
    level: { label: 'Grid level', scheme: () => ({ type: 'match', prop: 'level', entries: [
      [['LV', 'MV/LV'], pal().cat[0], 'LV and MV/LV station'], [['MV', 'HV/MV'], pal().cat[1], 'MV and HV/MV substation'],
      [['HV', 'EHV'], pal().cat[2], 'HV and EHV']], fallback: [pal().ink.neutral, 'Unknown'] }) } },
    layers: ({ id, colour, p }) => [{ id, type: 'circle', paint: {
      'circle-radius': ['interpolate', ['linear'], ['sqrt', ['coalesce', ['get', 'kw'], 0]], 0, 2.2, 10, 4, 32, 7, 100, 12],
      'circle-color': colour, 'circle-opacity': 0.85, 'circle-stroke-color': p.surface, 'circle-stroke-width': 1 } }],
    extraLegend: () => [{ kind: 'note', label: 'Circle area grows with the net power' }],
    facts: (o) => ({ title: `${o.technology || 'Unit'} · ${f.kw(o.kw)}`, subtitle: o.fid, rows: [['Grid level', o.level || '–'],
      ['Commissioned', o.year || '–'], ['Status', o.status || '–']] }),
  },
};

function theme3(p, light, dark) {
  return p.surface === '#ffffff' ? light : dark;
}

export function statusText(s) {
  return { converged: 'converged', voltage_violation: 'voltage violation', loading_violation: 'overloaded',
    not_converged: 'not converged', failed: 'failed', done: 'done' }[s] || (s || '–');
}

/** The scheme of a layer in `mode` (or null for layers with fixed colours). */
export function schemeOf(layerId, mode) {
  const modes = STYLES[layerId]?.modes;
  if (!modes) return null;
  const m = modes[mode] || Object.values(modes)[0];
  return m.scheme();
}
export function defaultMode(layerId) {
  const modes = STYLES[layerId]?.modes;
  return modes ? Object.keys(modes)[0] : null;
}

/** MapLibre layer definitions of one viewer layer. */
export function mapLayers(layerId, mode, d3 = false, hide = null) {
  const style = STYLES[layerId];
  const scheme = schemeOf(layerId, mode);
  const ctx = { id: layerId, mode: mode || defaultMode(layerId), scheme, colour: scheme ? expr(scheme) : null, d3, p: pal(), hide };
  return style.layers(ctx).map((l) => ({ source: layerId, ...l }));
}
