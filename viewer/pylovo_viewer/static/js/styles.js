// How each layer looks: its colour modes, its MapLibre layers, and the facts its tooltip and the
// inspector show. Colours come from schemes (palette.js), so the legend is derived from the same
// description as the map.
import { LOADING_STOPS, VOLTAGE_STOPS, constant, expr, pal, slot, voltageLevels } from './palette.js';
import * as f from './format.js';
import { arrowIcon, stationIcon } from './icons.js';

// Line width that grows exponentially with the zoom: the base width times a factor per zoom stop.
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
// Station symbols: always drawn, never hidden by other symbols.
const symbol = (image, size) => ({ 'icon-image': image, 'icon-size': size, 'icon-allow-overlap': true, 'icon-ignore-placement': true });

// The title of an HV/MV substation, its site and busbar: the level first, as their OSM names rarely say it.
const hvmv = (name) => (name ? `HV/MV: ${name}` : 'HV/MV substation');
// The ends of a feeder section.
const sectionEnd = (e) => ({ station: 'station', cabinet: `cabinet ${e?.id}` }[e?.kind] || 'end of the feeder');

// --------------------------------------------------------------------------------- schemes
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
  hv_lines: {
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
  hv_substations: {
    // badges in the surface colour (the reverse of the dark MV/LV ones) and larger at every zoom;
    // candidates the run did not use are faded
    layers: ({ id, p }) => [
      { id, type: 'symbol', layout: symbol(stationIcon('hvmv', p.surface, p.surface),
        ['interpolate', ['linear'], ['zoom'], 5, 0.5, 9, 0.75, 12, 0.95, 15, 1.2, 19, 1.6]),
        paint: { 'icon-opacity': ['case', ['get', 'used'], 1, 0.45] } },
      { id: `${id}:label`, type: 'symbol', pick: false, minzoom: 10,
        layout: { ...label(['coalesce', ['get', 'name'], ''], 11.5), 'text-anchor': 'top',
          'text-offset': ['interpolate', ['linear'], ['zoom'], 10, ['literal', [0, 1.7]], 19, ['literal', [0, 2.5]]] },
        paint: { 'text-color': p.ink.primary, 'text-halo-color': p.surface, 'text-halo-width': 1.8 } },
    ],
    // `colour` is the dot of the panel and the inspector, `fill` the badge
    legend: () => [{ kind: 'station', hv: true, fill: pal().surface, colour: pal().ink.primary, label: 'HV/MV substation (supplies an area)' },
      { kind: 'station', hv: true, faded: true, fill: pal().surface, colour: pal().ink.primary, label: 'Candidate, not used (faded)' }],
    facts: (o) => ({ title: hvmv(o.name), subtitle: `${o.source}${o.used ? '' : ' · not used'}`, rows: [
      ['Transformers', o.transformers ? `${o.transformers} × ${f.num(o.mva / Math.max(o.transformers, 1), 0)} MVA` : '–'],
      ['Loading (n-0 / n-1)', `${f.pct(o.loading)} / ${f.pct(o.loading_n1)}`], ['Voltage tag', o.voltage || '–'],
      ['eTraGo 110 kV bus', o.etrago_bus ?? 'none (no eGon match)'],
      ['Operator', o.operator || '–'], ...(o.flags ? [['Flags', o.flags]] : []),
      // after a click: the MV lines of its supply area (highlighted), see main.js
      ...(o.supplied_lines ? [['Supplies (area ' + o.supplied_area + ')', `${f.num(o.supplied_lines)} MV lines · ${f.km(o.supplied_km)}`],
        ['Rings / MV/LV substations', `${f.num(o.supplied_rings)} / ${f.num(o.supplied_stations)}`],
        ['Normally open (not highlighted)', `${f.count(o.open_ties, 'tie')} · ${f.count(o.open_links, 'link')}`]] : [])] }),
  },
  hv_sites: {
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': p.ink.primary, 'fill-opacity': 0.1 } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': p.ink.primary, 'line-width': 1.4 } },
    ],
    legend: () => [{ kind: 'fill', colour: pal().ink.primary, label: 'Substation site' }],
    facts: (o) => ({ title: hvmv(o.name), subtitle: `site · ${o.source}`, rows: [] }),
  },
  hv_busbars: {
    // the circle the MV lines of a substation end on (their display geometry), under the substation symbol
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': p.surface, 'fill-opacity': 0.95 } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': p.ink.primary,
        'line-width': ['interpolate', ['linear'], ['zoom'], 15, 1.5, 19, 3] } },
    ],
    legend: () => [{ kind: 'circle', hollow: true, colour: pal().ink.primary, label: 'Busbar: the MV lines end on it' }],
    facts: (o) => ({ title: hvmv(o.name), subtitle: 'busbar', rows: [] }),
  },
  mv_lines: {
    modes: { ring: { label: 'Ring', scheme: () => slot('ring_slot') },
      kind: { label: 'Cable / overhead', scheme: () => ({ type: 'match', prop: 'kind', entries: [
        ['cable', pal().cat[0], 'Cable'], ['overhead', pal().cat[1], 'Overhead line (dashed)'],
        ['mixed', pal().cat[2], 'Cable and overhead line (overhead parts dashed)']],
        fallback: [pal().ink.neutral, 'Other'] }) },
      role: { label: 'Role', scheme: () => ({ type: 'match', prop: 'role', entries: [
        ['ring', pal().cat[0], 'Open ring'], ['stub', pal().cat[2], 'Stub (T-off)'], ['dedicated', pal().cat[1], 'Dedicated feeder']],
        fallback: [pal().ink.neutral, 'Other'] }) },
      loading: { label: 'Loading (load case)', scheme: () => loading('loading', 'Line loading, load case') },
      loading_n1: { label: 'Loading (n-1)', scheme: () => loading('loading_n1', 'Line loading, n-1 case') },
      area: { label: 'Supply area', scheme: () => slot() } },
    layers: ({ id, colour, p }) => {
      const width = (add) => ['interpolate', ['exponential', 1.5], ['zoom'],
        ...[[8, 0.5], [12, 1.6], [16, 4]].flatMap(([z, k]) => [z, ['+', ['*', k, parallelWidth], add]])];
      const w = width(0);
      // ties (between supply areas) and links (between rings of one area) are normally open: dotted
      const open = ['in', ['get', 'role'], ['literal', ['tie', 'link']]];
      const notTie = ['!', open];
      return [
        { id: `${id}:tie`, type: 'line', filter: open,
          paint: { 'line-color': p.ink.muted, 'line-width': ['interpolate', ['linear'], ['zoom'], 8, 1, 14, 2.5], 'line-dasharray': [0.6, 1.6] } },
        { id: `${id}:casing`, type: 'line', pick: false, filter: notTie, layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': p.surface, 'line-width': width(1.6), 'line-opacity': 0.8 } },
        { id, type: 'line', filter: ['all', notTie, ['!=', ['get', 'kind'], 'overhead']], layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: { 'line-color': colour, 'line-width': w } },
        { id: `${id}:overhead`, type: 'line', filter: ['all', notTie, ['==', ['get', 'kind'], 'overhead']],
          paint: { 'line-color': colour, 'line-width': w, 'line-dasharray': [2.4, 1.2] } },
        // the overhead parts of mixed lines: their solid line is cleared there and drawn dashed
        { id: `${id}:mixed-clear`, type: 'line', source: `${id}:overhead`, pick: false, filter: notTie,
          paint: { 'line-color': p.surface, 'line-width': w } },
        { id: `${id}:mixed-overhead`, type: 'line', source: `${id}:overhead`, pick: false, filter: notTie,
          paint: { 'line-color': colour, 'line-width': w, 'line-dasharray': [2.4, 1.2] } },
      ];
    },
    overhead: true,
    extraLegend: (mode) => [
      ...(mode !== 'kind' ? [{ kind: 'line', colour: pal().ink.secondary, label: 'Cable' },
        { kind: 'line', dash: true, colour: pal().ink.secondary, label: 'Overhead line, or the overhead part of a line (dashed)' }] : []),
      { kind: 'line', dot: true, colour: pal().ink.muted, label: 'Tie between supply areas or link between rings (normally open)' }],
    facts: (o) => ({ title: `MV ${{ overhead: 'overhead line', mixed: 'cable and overhead line' }[o.kind] || 'cable'}`,
      subtitle: `${o.role}${o.open ? ' · normally open' : ''}`, rows: [
      ['Type', o.std_type || '–'], ['Length', f.km(o.length_km)],
      ...(o.kind === 'mixed' ? [['Cable / overhead', `${f.km(o.cable_km)} / ${f.km(o.overhead_km)}`]] : []),
      ['Parallel', o.parallel > 1 ? `${o.parallel}×` : '1'],
      ['Ring / feeder', o.ring === null || o.ring === undefined ? '–' : `${o.ring} / ${o.feeder ?? '–'}`],
      ['Loading (load case)', f.pct(o.loading)], ['Loading (n-1)', f.pct(o.loading_n1)],
      ['Supply area', o.area],
      // a clicked line stands for its ring (see main.js)
      ...(o.ring_lines ? [['Ring with stubs', `${f.num(o.ring_lines)} lines · ${f.km(o.ring_km)} + ${f.km(o.stub_km)} stubs`],
        ['Overhead in the ring', f.km(o.ring_overhead_km)], ['Stations on the ring', f.num(o.ring_stations)],
        ['Max loading of the ring (n-1)', Number.isFinite(o.ring_max_n1) ? f.pct(o.ring_max_n1) : '–']] : [])] }),
  },
  mv_osm_lines: {
    layers: ({ id, p }) => [
      { id, type: 'line', layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': p.ink.neutral, 'line-opacity': 0.55,
          'line-width': ['interpolate', ['exponential', 1.5], ['zoom'], 8, 2, 12, 5, 16, 10] } },
    ],
    legend: () => [{ kind: 'line', colour: pal().ink.neutral, label: 'OSM MV overhead line (corridor)' }],
    facts: (o) => ({ title: 'OSM MV line', subtitle: o.osm_way || '', rows: [['OSM type', o.osm_type || '–'],
      ['Length', f.km(o.length_km)]] }),
  },
  mv_open_points: {
    layers: ({ id, p }) => [
      { id, type: 'circle', paint: { 'circle-radius': radius(4.4), 'circle-color': p.surface,
        'circle-stroke-color': p.ink.primary, 'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 11, 1.5, 16, 2.6] } },
    ],
    legend: () => [{ kind: 'circle', hollow: true, colour: pal().ink.primary, label: 'Normally-open point' }],
    facts: (o) => ({ title: 'Normally-open point', subtitle: `ring ${o.ring} · supply area ${o.area}`, rows: [['Opens at node', o.node]] }),
  },
  mv_stations: {
    modes: { plain: { label: 'Plain', scheme: () => constant(pal().ink.secondary, 'MV/LV substation') },
      ring: { label: 'MV ring', scheme: () => slot('ring_slot') },
      kva: { label: 'Rated power', scheme: kvaRamp },
      origin: { label: 'Origin', scheme: () => ({ type: 'match', prop: 'origin', entries: [
        ['new', pal().cat[0], 'New (greenfield)'], ['osm', pal().cat[1], 'Existing (OSM)'], ['lod2', pal().cat[2], 'Existing (LoD2)']],
        fallback: [pal().ink.neutral, 'Other'] }) } },
    legendKind: 'station',
    layers: ({ id, colour, p }) => [
      // badge size grows with the zoom and a little with the rated power
      { id, type: 'symbol', layout: symbol(stationIcon('mvlv', colour, p.surface),
        ['interpolate', ['linear'], ['zoom'], ...[[10.2, 0.4], [12, 0.6], [14, 0.9], [16, 1.15], [19, 1.5]].flatMap(([z, k]) =>
          [z, ['*', k, ['interpolate', ['linear'], ['coalesce', ['get', 'kva'], 250], 100, 0.85, 400, 0.95, 630, 1.05, 1260, 1.25]]])]) },
      { id: `${id}:label`, type: 'symbol', pick: false, minzoom: 15,
        layout: { ...label(['concat', ['to-string', ['get', 'kva']], ' kVA'], 11), 'text-offset': [0, 1.8], 'text-anchor': 'top' },
        paint: { 'text-color': p.ink.primary, 'text-halo-color': p.surface, 'text-halo-width': 1.6 } },
    ],
    facts: (o) => ({ title: `MV/LV substation of grid ${o.fid}`, subtitle: `PLZ ${o.plz} · cluster ${o.kcid}/${o.bcid}`, rows: [
      ['Rated power', f.kva(o.kva)],
      ['Origin', { new: 'new (greenfield)', osm: 'existing (OSM)', lod2: 'existing (LoD2)' }[o.origin] || '–'],
      ...(o.area !== null && o.area !== undefined ? [['MV supply area / ring', `${o.area} / ${o.ring ?? '–'}`],
        ['MV voltage (load case)', f.pu(o.vm)], ['LV busbar voltage', f.pu(o.lv_vm)]] : []),
      // after a click: the cables of its LV grid (highlighted), see main.js
      ...(o.supplied_cables ? [['Supplies', `${f.count(o.supplied_cables, 'cable')} · ${f.km(o.supplied_feeder_km)} feeders + ${f.km(o.supplied_service_km)} service`],
        ['Feeders / buildings', `${o.supplied_feeders ?? '–'} / ${f.num(o.supplied_buildings)}`]] : [])] }),
  },
  mv_areas: {
    modes: { area: { label: 'Supply area', scheme: () => slot() },
      voltage: { label: 'Voltage', scheme: () => ({ type: 'match', prop: 'vn_kv', entries: [[10, pal().cat[1], '10 kV'], [20, pal().cat[0], '20 kV']],
        fallback: [pal().ink.neutral, 'Other'] }) } },
    layers: ({ id, colour, p }) => [
      { id, type: 'fill', paint: { 'fill-color': colour,
        'fill-opacity': zoomed([[8, 0.13], [11, 0.07], [14, 0.025]], (v) => hover(0.2, v)) } },
      { id: `${id}:line`, type: 'line', pick: false, filter: ['!=', ['get', 'complete'], false],
        paint: { 'line-color': selected(p.accent, p.ink.secondary), 'line-width': selected(3.5, 1.2), 'line-opacity': 0.85 } },
      { id: `${id}:open`, type: 'line', pick: false, filter: ['==', ['get', 'complete'], false],
        paint: { 'line-color': selected(p.accent, p.ink.secondary), 'line-width': selected(3.5, 1.2), 'line-opacity': 0.85, 'line-dasharray': [3, 2] } },
    ],
    extraLegend: () => [{ kind: 'line', dash: true, colour: pal().ink.secondary, label: 'Incomplete (cut by the region border)' }],
    facts: (o) => ({ title: `Supply area ${o.fid}`, subtitle: hvmv(o.substation), rows: [
      ['Voltage', f.kv(o.vn_kv)], ['MV/LV stations', f.num(o.stations)], ['MV customers', f.num(o.customers)],
      ['Rings', f.num(o.rings)], ['Peak load', f.mw(o.peak_mw)], ['Max loading', f.pct(o.max_loading)],
      ['Max voltage drop', f.pct(o.max_drop, 2)], ['Complete', o.complete ? 'yes' : 'no (border)']] }),
  },
  mv_customers: {
    layers: ({ id, p }) => [
      { id, type: 'fill', paint: { 'fill-color': p.cat[1], 'fill-opacity': 0.22 } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': p.cat[1], 'line-width': 1.6 } },
    ],
    legend: () => [{ kind: 'fill', colour: pal().cat[1], label: 'Building supplied from MV' }],
    facts: (o) => ({ title: 'MV customer building', subtitle: o.fid, rows: [['Use', o.nonres_use || o.use || '–'],
      ['Peak load', f.kw(o.peak_kw)], ['Households', f.num(o.households)]] }),
  },
  lv_areas: {
    modes: { grid: { label: 'Grid', scheme: () => slot() },
      kva: { label: 'Station size', scheme: kvaRamp } },
    layers: ({ id, colour }) => [
      { id, type: 'fill', paint: { 'fill-color': colour,
        'fill-opacity': zoomed([[12, 0.2], [15, 0.07], [17, 0.03]], (v) => hover(0.34, selected(Math.max(v, 0.12), v))) } },
      { id: `${id}:line`, type: 'line', pick: false, paint: { 'line-color': selected(pal().accent, colour), 'line-width': selected(3.5, 1),
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 11, 0.5, 15, 0.9] } },
    ],
    facts: (o) => ({ title: `LV grid ${o.fid}`, subtitle: `PLZ ${o.plz} · cluster ${o.kcid}/${o.bcid}`, rows: [
      ['Station', f.kva(o.kva)], ['Buildings', f.num(o.buildings)]] }),
  },
  lv_cables: {
    modes: { grid: { label: 'Grid', scheme: () => slot() }, feeder: { label: 'Feeder', scheme: feeders },
      type: { label: 'Cable type', scheme: () => ({ type: 'ramp', prop: 'mm2', label: 'Cross-section', unit: 'mm²',
        stops: [[50, pal().slate[0]], [95, pal().slate[1]], [150, pal().slate[2]], [185, pal().slate[3]], [240, pal().slate[4]], [300, pal().slate[5]]] }) },
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
      // the arrow at the end of the cable that supplies a cabinet, tip at the cabinet's rim
      { id: `${id}:arrow`, type: 'symbol', source: `${id}:arrows`, pick: false, minzoom: 15,
        layout: { ...symbol(arrowIcon(colour, p.surface), ['interpolate', ['linear'], ['zoom'], 15, 1.1, 18, 1.6]),
          'icon-anchor': 'top', 'icon-offset': [0, 5], 'icon-rotate': ['get', 'bearing'],
          'icon-rotation-alignment': 'map', 'icon-pitch-alignment': 'map' } },
    ],
    arrows: true,
    extraLegend: (mode) => [...(mode === 'type' ? [{ kind: 'line', colour: pal().ink.muted, label: 'Service cable', thin: true }] : []),
      { kind: 'note', label: 'Arrow: the cable that supplies a cabinet' }],
    // a clicked feeder cable stands for its section (from a split point to the next), see main.js
    facts: (o) => (o.section_cables ? { title: 'Feeder section', subtitle: `${sectionEnd(o.from)} → ${sectionEnd(o.to)}`, rows: [
      ['Length', f.meters(o.section_m)], ['Cables', o.types.map(([t, m]) => `${prettyCable(t)} (${f.meters(m)})`).join(', ')],
      ['House connections', f.num(o.connections)], ['Feeder', o.feeder ? `#${o.feeder}` : '–'], ['Grid', o.grid]] }
      : { title: prettyCable(o.std_type), subtitle: o.role === 'service' ? 'Service cable' : 'Feeder cable', rows: [
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
};

function theme3(p, light, dark) {
  return p.surface === '#ffffff' ? light : dark;
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
