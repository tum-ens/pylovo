// The viewer app: database, version and MV run, one switch per voltage level (with optional detail
// per layer), regions for navigation and key figures, search, inspector, legend, 3D and the URL hash.
// Map drawing lives in map.js. LV detail and LoD2 always follow the map view, and so does a layer that
// is too large to load whole (the API answers `by_view`, e.g. all MV lines of Bavaria). Every request
// names the database of this page (`db`), so tabs on different databases never mix.
import { createApp, markRaw } from 'vue';
import { getJson } from './api.js';
import * as f from './format.js';
import { BASEMAPS, MapView, Z_ORDER, boundsOf } from './map.js';
import { colourOf, gradient, pal } from './palette.js';
import { STYLES, defaultMode, schemeOf } from './styles.js';
import { showLod2 } from './lod2.js';
import { stationColours } from './icons.js';

const EMPTY = { type: 'FeatureCollection', features: [] };
const REGION_SOURCE = { plz: 'postcodes', area: 'mv_areas', grid: 'lv_areas' };
const GRID_LAYERS = new Set(['lv_areas', 'mv_stations']);
const SUPPLYING = new Set(['mv_stations', 'hv_substations']);   // a click highlights the lines they supply
const KIND = { point: 'circle', line: 'line', polygon: 'fill' };
const LOD2_ZOOM = 15;
const VIEW_ZOOM = 10;   // a layer too large to load whole follows the map view from this zoom
const STORE = 'pylovo-viewer';

function readHash() {
  const out = {};
  for (const part of location.hash.replace(/^#/, '').split('&')) {
    const [k, v] = part.split('=');
    if (k && v !== undefined) out[k] = decodeURIComponent(v);
  }
  return out;
}
function stored(key, fallback) {
  try { return localStorage.getItem(`${STORE}.${key}`) ?? fallback; } catch { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem(`${STORE}.${key}`, value); } catch { /* private mode */ }
}
const inside = (a, b) => b && a[0] >= b[0] && a[1] >= b[1] && a[2] <= b[2] && a[3] <= b[3];

createApp({
  data() {
    return {
      fatal: null, info: null, db: null, databases: [], meta: [], groupMeta: [], lod2Available: false, mvVariants: false,
      versions: [],
      version: null, mv: null, region: null, layers: {}, groups: {}, zoom: 5,
      theme: stored('theme', 'auto'), basemap: 'streets', d3: true,
      tab: 'layers', panel: true, legendOpen: true, help: false,
      search: { text: '', hits: [], open: false, index: 0 }, tooltip: null, inspector: null,
      summary: null, postcodes: [], areas: [], regionFilter: '', lod2: null, toast: null, tick: 0, themeTick: 0,
      basemaps: Object.fromEntries(Object.entries(BASEMAPS).map(([k, v]) => [k, v.label])),
    };
  },
  computed: {
    fmt() { return f; },
    metaById() { return Object.fromEntries(this.meta.map((m) => [m.id, m])); },
    runs() { return this.versions.find((v) => v.version_id === this.version)?.runs || []; },
    run() { return this.runs.find((r) => r.id === this.mv) || null; },
    groupList() {
      return this.groupMeta.map((g) => {
        const layers = this.meta.filter((m) => m.groups.includes(g.id) && !m.parent);
        const available = layers.filter((m) => m.available);
        return { ...g, layers, available: available.length > 0,
          reason: available.length ? '' : (layers[0]?.reason || 'no data in this database') };
      }).filter((g) => g.layers.length);
    },
    regionLabel() {
      if (!this.region) return null;
      const { kind, id } = this.region;
      return { plz: `Postcode ${id}`, area: `Supply area ${id}`, grid: `LV grid ${id}` }[kind];
    },
    filteredPostcodes() {
      const q = this.regionFilter.trim().toLowerCase();
      return q ? this.postcodes.filter((p) => `${p.plz} ${p.name || ''}`.toLowerCase().includes(q)) : this.postcodes;
    },
    legend() {
      this.tick; this.themeTick;   // eslint-disable-line no-unused-expressions
      const out = [];
      for (const id of [...Z_ORDER].reverse()) {
        const m = this.metaById[id];
        // what is drawn, whatever the state of its latest request (a reload keeps the data until it is answered)
        if (!m || !this.visible(id) || !this.map?.data(id)?.features.length || this.zoom < (m.min_zoom || 0)) continue;
        const style = STYLES[id];
        const entries = [...(style.legend ? style.legend() : [])];
        const scheme = schemeOf(id, this.modeOf(id));
        const kind = style.legendKind || KIND[m.geometry];
        if (scheme?.type === 'match') {
          const counts = new Map();
          for (const ft of this.map?.data(id)?.features || []) {
            const v = ft.properties[scheme.prop] ?? null;
            counts.set(v, (counts.get(v) || 0) + 1);
          }
          let rest = [...counts.values()].reduce((a, b) => a + b, 0);
          for (const [value, colour, label] of scheme.entries) {
            const n = (Array.isArray(value) ? value : [value]).reduce((sum, v) => sum + (counts.get(v) || 0), 0);
            rest -= n;
            if (n) entries.push({ kind, colour, label, count: n });
          }
          if (rest > 0) entries.push({ kind, colour: scheme.fallback[0], label: scheme.fallback[1], count: rest });
        } else if (scheme?.type === 'ramp') {   // identity colours (slots: grid, ring, area) need no legend
          entries.push({ kind: 'ramp', css: gradient(scheme.stops), min: scheme.stops[0][0], max: scheme.stops[scheme.stops.length - 1][0],
            unit: scheme.unit, label: scheme.label });
        } else if (scheme?.type === 'const') {
          entries.push({ kind, colour: scheme.colour, label: scheme.label });
        }
        if (style.extraLegend) entries.push(...style.extraLegend(this.modeOf(id)));
        for (const e of entries) if (e.kind === 'station') Object.assign(e, stationColours(e.fill || e.colour, pal().surface, e.hv ? 'hvmv' : 'mvlv'));
        if (entries.length) out.push({ id, title: m.title, entries });
      }
      return out;
    },
  },
  watch: {
    theme(value) { store('theme', value); this.applyTheme(); },
    // not remembered between visits (the URL keeps it), so a new visit starts on Streets
    basemap() { this.map?.setBasemap(this.effectiveBasemap()); this.saveHash(); },
  },
  async mounted() {
    this.applyTheme();
    window.addEventListener('keydown', (e) => this.key(e));
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this.applyTheme());
    const h = readHash();
    try {
      const [info, list] = await Promise.all([getJson('/api/info'), getJson('/api/databases')]);
      Object.assign(this, { info, databases: list.databases });
      this.db = list.databases.includes(h.db) ? h.db : list.default;
      try {
        await this.openDatabase();
      } catch (err) {
        if (this.db === list.default) throw err;
        this.notify(`${this.db}: ${err.message}`);
        this.db = list.default;
        await this.openDatabase();
      }
    } catch (err) {
      this.fatal = err.message;
      return;
    }
    this.loaded = {};
    this.pending = {};
    const groupsOn = h.g !== undefined ? new Set(h.g.split(',')) : null;
    for (const g of this.groupMeta) this.groups[g.id] = { on: groupsOn ? groupsOn.has(g.id) : g.default, open: false };
    const wanted = h.l ? new Set(h.l.split(',')) : null;
    const modes = Object.fromEntries((h.m || '').split(',').filter(Boolean).map((x) => x.split(':')));
    for (const m of this.meta) {
      this.layers[m.id] = { on: wanted && !m.parent ? wanted.has(m.id) : m.default, mode: modes[m.id] || defaultMode(m.id),
        status: 'idle', count: null, note: '', truncated: false, byView: null, inView: false };
    }
    this.version = this.versions.some((v) => v.version_id === h.v) ? h.v : this.versions[0]?.version_id || null;
    this.mv = this.runs.some((r) => r.id === h.mv) ? h.mv : this.defaultRun();
    if (h.b && (BASEMAPS[h.b] || h.b === 'auto')) this.basemap = h.b;
    if (h.r) {
      const [kind, id] = h.r.split(':');
      if (REGION_SOURCE[kind] && id) this.region = { kind, id: Number(id) };
    }
    const c = (h.c || '').split(',').map(Number);
    const view = c.length >= 3 && c.every(Number.isFinite) ? { center: [c[0], c[1]], zoom: c[2], pitch: c[3] || 0, bearing: c[4] || 0 } : null;
    this.d3 = h['3d'] !== '0';
    this.map = markRaw(new MapView(this.$refs.map, {
      basemap: this.effectiveBasemap(), view, d3: this.d3,
      onMove: (v) => this.moved(v), onHover: (hit) => this.hovered(hit), onClick: (hits) => this.clicked(hits),
      onReady: () => this.ready(!view),
    }));
  },
  methods: {
    // ---------------------------------------------------------------------------- start-up
    ready(fit) {
      for (const m of this.meta) {
        this.map.setLayer(m.id, { visible: this.visible(m.id), mode: this.modeOf(m.id), attribution: m.attribution, minZoom: m.min_zoom });
      }
      this.zoom = this.map.view().zoom;
      this.loadLists();
      this.reload();
      this.loadSummary();
      if (this.region) this.map.select(REGION_SOURCE[this.region.kind], this.region.id);
      if (fit) this.flyTo(this.region || null);
      this.refreshLod2();
    },
    applyTheme() {
      const dark = this.theme === 'dark' || (this.theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      if (!this.map) return;
      if (!this.map.setBasemap(this.effectiveBasemap())) this.map.restyle();
      this.themeTick++;
      this.refreshLod2(true);
    },
    effectiveBasemap() {
      if (this.basemap !== 'auto') return this.basemap;
      return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
    },

    // ------------------------------------------------------------------------------- layers
    /** Shown on the map: available, and its parent, or the layer itself in one of its groups, on. */
    visible(id) {
      const m = this.metaById[id];
      if (!m?.available) return false;
      if (m.parent) return this.visible(m.parent);
      return !!this.layers[id]?.on && m.groups.some((g) => this.groups[g]?.on);
    },
    modeOf(id) {
      return this.layers[this.metaById[id]?.parent || id]?.mode;
    },
    children(id) {
      return this.meta.filter((m) => m.parent === id).map((m) => m.id);
    },
    applyVisibility() {
      for (const m of this.meta) {
        const on = this.visible(m.id);
        this.map.setLayer(m.id, { visible: on });
        if (on) this.load(m.id); else this.layers[m.id].status = 'idle';
      }
      this.refreshLod2();
      this.tick++;
      this.saveHash();
    },
    toggleGroup(gid) {
      this.groups[gid].on = !this.groups[gid].on;
      this.applyVisibility();
    },
    toggle(id, gid) {
      this.layers[id].on = !this.layers[id].on;
      if (this.layers[id].on && !this.groups[gid].on) this.groups[gid].on = true;
      this.applyVisibility();
    },
    setMode(id, mode) {
      this.layers[id].mode = mode;
      for (const child of [id, ...this.children(id)]) this.map.setLayer(child, { mode });
      if (id === 'lv_buildings') this.refreshLod2(true);
      this.tick++;
      this.saveHash();
    },
    viewBbox() {
      return this.map.bbox(0.25).map((v) => Number(v.toFixed(4))).join(',');
    },
    params(m) {
      const need = (cond, note) => (cond ? null : note);
      const bbox = () => this.viewBbox();
      const zoomed = () => need(this.zoom >= m.min_zoom, `Zoom in to see them (from zoom ${m.min_zoom})`);
      switch (m.scope) {
        case 'version':
          return need(this.version, 'Choose a version') || { version: this.version, mv: m.with_run ? this.mv : null };
        case 'view':
          return need(this.version, 'Choose a version') || zoomed() || { version: this.version, bbox: bbox() };
        case 'run':
          return need(this.version && this.mv, 'No MV run for this version') || { version: this.version, mv: this.mv };
        default:
          return {};
      }
    },
    /** A layer that cannot be loaded now: empty, with the reason. */
    wait(id, note) {
      Object.assign(this.layers[id], { status: 'wait', note, count: null });
      if (this.map.data(id)?.features.length) { this.map.setData(id, EMPTY); this.loaded[id] = null; this.tick++; }
    },
    async load(id, force = false) {
      const m = this.metaById[id];
      const st = this.layers[id];
      if (!m || !this.visible(id)) return;
      let params = this.params(m);
      if (typeof params === 'string') { this.wait(id, params); return; }
      const base = JSON.stringify({ db: this.db, ...params, bbox: undefined });
      if (!params.bbox && st.byView === base) {   // too large to load whole: the map view only
        const from = Math.max(VIEW_ZOOM, m.min_zoom || 0);
        if (this.zoom < from) { this.wait(id, `Too many to show at once: zoom in (from zoom ${from})`); return; }
        params = { ...params, bbox: this.viewBbox() };
      }
      const { bbox } = params;
      const last = this.loaded[id];
      // a complete load of a larger box still holds the view; a cut one never does
      if (!force && last && last.base === base && !last.truncated && (!bbox || inside(this.map.bbox(0), last.bbox))) return;
      const token = `${base}|${bbox || ''}`;
      this.pending[id] = token;
      st.status = 'loading';
      try {
        const fc = await this.api(`/api/layer/${id}`, params);
        if (this.pending[id] !== token) return;
        if (fc.by_view) { st.byView = base; this.load(id, true); return; }
        if (fc.zoom_in) { this.wait(id, 'Too many in this view: zoom in'); this.loaded[id] = null; return; }
        this.map.setData(id, fc);
        this.loaded[id] = { base, bbox: bbox ? bbox.split(',').map(Number) : null, truncated: fc.truncated };
        Object.assign(st, { status: 'ok', count: fc.features.length, truncated: fc.truncated,
          inView: !!bbox && m.scope !== 'view', note: '' });
        this.tick++;
        if (id === 'lv_buildings') this.refreshLod2(true);
      } catch (err) {
        if (this.pending[id] !== token) return;
        Object.assign(st, { status: 'error', note: err.message });
      }
    },
    /** Load the visible layers of the given scopes (all when null); `withRun` adds the run-aware ones. */
    reload(scopes = null, force = false, withRun = false) {
      for (const m of this.meta) {
        if (!scopes || scopes.includes(m.scope) || (withRun && m.with_run)) this.load(m.id, force);
      }
    },
    swatch(id) {
      this.themeTick;   // eslint-disable-line no-unused-expressions
      const scheme = schemeOf(id, this.layers[id]?.mode);
      if (!scheme) return STYLES[id]?.legend?.()[0]?.colour || pal().ink.secondary;
      if (scheme.type === 'slot') return `conic-gradient(${pal().cat.slice(0, 4).join(',')})`;
      if (scheme.type === 'ramp') return gradient(scheme.stops);
      if (scheme.type === 'match') return scheme.entries[0]?.[1] || scheme.fallback[0];
      return scheme.colour;
    },
    modes(id) {
      return Object.entries(STYLES[id]?.modes || {}).map(([k, v]) => ({ id: k, label: v.label }));
    },
    groupNote(g) {
      if (!g.available) return g.reason;
      if (g.id === 'mv' && !this.mvVariants) return 'No MV variants in this database';
      if (!this.groups[g.id]?.on) return g.description;
      const loading = g.layers.some((m) => this.layers[m.id]?.status === 'loading');
      const waiting = g.layers.filter((m) => this.visible(m.id) && this.layers[m.id]?.status === 'wait');
      const cut = this.groupCut(g);
      if (loading) return 'loading …';
      if (cut.length) return `${cut.map((m) => m.title).join(' and ')}: only the first ${f.num(this.layers[cut[0].id].count)} shown, zoom in`;
      if (waiting.length) return `${waiting.map((m) => m.title).join(' and ')}: ${this.layers[waiting[0].id].note.toLowerCase()}`;
      return g.description;
    },
    /** The shown layers of a group that hold more features than were sent. */
    groupCut(g) {
      if (!this.groups[g.id]?.on) return [];
      return g.layers.filter((m) => this.visible(m.id) && this.layers[m.id]?.status === 'ok' && this.layers[m.id].truncated);
    },

    // ---------------------------------------------------------------------------- selection
    setVersion(version) {
      this.version = version;
      this.mv = this.defaultRun();
      this.region = null;
      this.map.select(null, null);
      this.closeInspector();
      this.reload(null, true);
      this.loadLists();
      this.loadSummary();
      this.flyTo(null);
      this.refreshLod2(true);
      this.saveHash();
    },
    // ----------------------------------------------------------------------------- database
    /** GET from the viewer's API, always for the database of this page. */
    api(path, params = {}) {
      return getJson(path, { db: this.db, ...params });
    },
    /** Read what the database of this page holds: its layers and its versions. */
    async openDatabase() {
      const db = this.db;
      const [catalog, versions] = await Promise.all([this.api('/api/catalog'), this.api('/api/versions')]);
      if (this.db !== db) return false;   // switched again meanwhile
      Object.assign(this, { meta: catalog.layers, groupMeta: catalog.groups, lod2Available: catalog.lod2,
        mvVariants: catalog.mv_variants, versions });
      return true;
    },
    /** Show another database of the server: its newest version and MV run, the whole version in view. */
    async setDatabase(db) {
      const previous = this.db;
      this.db = db;
      try {
        if (!await this.openDatabase()) return;
      } catch (err) {
        this.notify(`${db}: ${err.message}`);
        if (this.db === db) this.db = previous;
        return;
      }
      this.region = null;
      this.map.select(null, null);
      this.closeInspector();
      for (const m of this.meta) {
        this.layers[m.id] ||= { on: m.default, mode: defaultMode(m.id), status: 'idle', count: null, note: '', truncated: false, byView: null, inView: false };
        this.map.setLayer(m.id, { attribution: m.attribution, minZoom: m.min_zoom });
        this.map.setData(m.id, EMPTY);
      }
      this.version = this.versions[0]?.version_id || null;
      this.mv = this.defaultRun();
      this.loaded = {};
      this.lod2Bbox = null;
      this.applyVisibility();
      this.loadLists();
      this.loadSummary();
      this.flyTo(null);
      this.refreshLod2(true);
    },

    /** The newest MV run of the current MV stage version, else the newest. */
    defaultRun() {
      return (this.runs.find((r) => !r.older) || this.runs[0])?.id || null;
    },
    /** The note on a run of an older MV stage version (read only in pylovo), or ''. */
    olderNote(run) {
      return run?.older ? `Planned with MV stage version ${run.stage_version ?? '(not recorded)'}: pylovo keeps it read only, and its results are shown as stored.` : '';
    },
    setRun(mv) {
      this.mv = mv || null;
      if (this.region?.kind === 'area') { this.region = null; this.map.select(null, null); }
      this.reload(['run'], true, true);
      this.loadLists();
      this.loadSummary();
      this.saveHash();
    },
    /** Select a region: fly there, mark it and show its key figures (the layers follow the view). */
    selectRegion(kind, id, { fly = true } = {}) {
      this.region = { kind, id };
      this.map.select(REGION_SOURCE[kind], id);
      this.loadSummary();
      if (fly) this.flyTo(this.region);
      this.saveHash();
    },
    clearRegion() {
      this.region = null;
      this.map.select(null, null);
      this.loadSummary();
      this.saveHash();
    },
    async flyTo(region) {
      if (!this.version) return;
      const db = this.db;
      const params = { version: this.version, mv: region ? (region.kind === 'area' ? this.mv : null) : null };
      if (region) params[region.kind] = region.id;
      try {
        const { bounds } = await this.api('/api/bounds', params);
        if (this.db === db) this.map.fit(bounds, this.padding(), region ? 16 : 13);
      } catch (err) {
        this.notify(err.message);
      }
    },
    padding() {
      const wide = window.innerWidth > 760;
      return { top: 80, bottom: 50, left: wide && this.panel ? 380 : 30, right: wide && this.inspector ? 400 : 60 };
    },
    async loadLists() {
      this.postcodes = [];
      this.areas = [];
      if (!this.version) return;
      const key = `${this.db}|${this.version}|${this.mv}`;
      this.listsKey = key;
      try {
        const postcodes = await this.api('/api/postcodes', { version: this.version });
        const areas = this.mv ? await this.api('/api/areas', { version: this.version, mv: this.mv }) : [];
        if (this.listsKey === key) Object.assign(this, { postcodes, areas });
      } catch (err) {
        this.notify(err.message);
      }
    },
    async loadSummary() {
      if (!this.version) { this.summary = null; return; }
      const params = { db: this.db, version: this.version, mv: this.mv };
      if (this.region) params[this.region.kind] = this.region.id;
      const key = JSON.stringify(params);
      this.summaryKey = key;
      try {
        const data = await getJson('/api/summary', params);
        if (this.summaryKey === key) this.summary = data;
      } catch (err) {
        if (this.summaryKey === key) this.summary = null;
      }
    },

    // ----------------------------------------------------------------------- map interaction
    moved(view) {
      this.zoom = view.zoom;
      this.tooltip = null;
      clearTimeout(this.moveTimer);
      this.moveTimer = setTimeout(() => {
        for (const m of this.meta) if (m.scope === 'view' || this.layers[m.id]?.byView) this.load(m.id);
        this.refreshLod2();
        this.saveHash();
      }, 250);
    },
    facts(hit) {
      const style = STYLES[hit.layer];
      const props = hit.group ? { ...hit.props, ...hit.group } : hit.props;
      const facts = style?.facts ? style.facts(props) : { title: hit.layer, rows: [] };
      const scheme = schemeOf(hit.layer, this.modeOf(hit.layer));
      return { ...facts, colour: scheme ? colourOf(scheme, hit.props) : style?.legend?.()[0]?.colour, layerTitle: this.metaById[hit.layer]?.title };
    },
    hovered(hit) {
      if (!hit) { this.tooltip = null; return; }
      const facts = this.facts(hit);
      this.tooltip = { x: hit.x, y: hit.y, ...facts, rows: facts.rows.slice(0, 4) };
    },
    clicked(hits) {
      if (!hits.length) { this.closeInspector(); return; }
      this.inspector = { hits: hits.slice(0, 8), index: 0, attrs: null, attrsOpen: false, loading: false, error: null };
      this.show(0);
    },
    show(index) {
      const ins = this.inspector;
      ins.index = index;
      ins.attrs = null;
      ins.error = null;
      const hit = ins.hits[index];
      hit.group ||= this.ringOf(hit);
      this.map.setHighlight(hit.group || this.map.feature(hit.layer, hit.fid), hit.group?.open_points);
      if (ins.attrsOpen) this.loadAttrs();
      if (hit.layer === 'lv_cables' && hit.props.role === 'feeder' && !hit.group) this.loadSection(ins, hit);
      if (SUPPLYING.has(hit.layer) && !hit.group) this.loadSupplied(ins, hit);
    },
    /** A transformer stands for the lines it supplies: the cables of its LV grid, or the MV lines of its area. */
    async loadSupplied(ins, hit) {
      try {
        const supplied = await this.api(`/api/supplied/${hit.layer}/${encodeURIComponent(hit.fid)}`, { version: this.version, mv: this.mv });
        if (!supplied.geometry) return;
        hit.group = supplied;
        if (this.inspector === ins && ins.hits[ins.index] === hit) this.map.setHighlight(supplied);
      } catch (err) {
        this.notify(err.message);
      }
    },
    /** A feeder cable stands for its section, from a split point (or the station) to the next. */
    async loadSection(ins, hit) {
      try {
        const section = await this.api(`/api/section/${encodeURIComponent(hit.fid)}`);
        hit.group = section;
        if (this.inspector === ins && ins.hits[ins.index] === hit) this.map.setHighlight(section);
      } catch (err) {
        this.notify(err.message);
      }
    },
    /** An MV line stands for its whole ring (ring and stub lines of the same ring), with the open points. */
    ringOf(hit) {
      const p = hit.props;
      if (hit.layer !== 'mv_lines' || p.role === 'tie' || p.ring === null || p.ring === undefined) return null;
      const same = (ft) => ft.properties.area === p.area && ft.properties.ring === p.ring;
      const lines = (this.map.data('mv_lines')?.features || []).filter((ft) => same(ft) && ft.properties.role !== 'tie');
      const km = (role, key = 'length_km') => lines.filter((ft) => !role || ft.properties.role === role)
        .reduce((sum, ft) => sum + (ft.properties[key] || 0), 0);
      return {
        geometry: { type: 'MultiLineString', coordinates: lines.flatMap((ft) => (ft.geometry.type === 'LineString' ? [ft.geometry.coordinates] : ft.geometry.coordinates)) },
        open_points: (this.map.data('mv_open_points')?.features || []).filter(same).map((ft) => ft.geometry),
        ring_lines: lines.length, ring_km: km('ring'), stub_km: km('stub'), ring_overhead_km: km(null, 'overhead_km'),
        ring_stations: (this.map.data('mv_stations')?.features || []).filter(same).length,
        ring_max_n1: Math.max(...lines.map((ft) => ft.properties.loading_n1 ?? -Infinity)),
      };
    },
    async loadAttrs() {
      const ins = this.inspector;
      if (!ins) return;
      ins.attrsOpen = true;
      const hit = ins.hits[ins.index];
      if (this.metaById[hit.layer]?.scope === 'file') { ins.attrs = Object.entries(hit.props); return; }
      ins.loading = true;
      try {
        const row = await this.api(`/api/feature/${hit.layer}/${encodeURIComponent(hit.fid)}`, { version: this.version, mv: this.mv });
        if (this.inspector === ins && ins.hits[ins.index] === hit) ins.attrs = Object.entries(row);
      } catch (err) {
        ins.error = err.message;
      } finally {
        ins.loading = false;
      }
    },
    closeInspector() {
      this.inspector = null;
      this.map?.setHighlight(null);
    },
    actions(hit) {
      const p = hit.props;
      const out = [];
      const feature = hit.group || this.map.feature(hit.layer, hit.fid);
      if (feature) out.push({ label: 'Zoom to', run: () => this.map.fit(boundsOf(feature.geometry), this.padding(), 18) });
      const grid = GRID_LAYERS.has(hit.layer) ? p.fid : p.grid;
      if (grid !== null && grid !== undefined && !(this.region?.kind === 'grid' && this.region.id === grid)) {
        out.push({ label: 'Select this LV grid', run: () => this.selectRegion('grid', grid) });
      }
      const area = hit.layer === 'mv_areas' ? p.fid : p.area;
      if (area !== null && area !== undefined && this.mv && !(this.region?.kind === 'area' && this.region.id === area)) {
        out.push({ label: 'Select supply area', run: () => this.selectRegion('area', area) });
      }
      if (hit.layer === 'postcodes' && !(this.region?.kind === 'plz' && this.region.id === p.fid)) {
        out.push({ label: 'Select postcode', run: () => this.selectRegion('plz', p.fid) });
      }
      return out;
    },
    copyAttrs() {
      const ins = this.inspector;
      const data = Object.fromEntries(ins.attrs || Object.entries(ins.hits[ins.index].props));
      navigator.clipboard?.writeText(JSON.stringify(data, null, 2)).then(() => this.notify('Copied as JSON', 'info'));
    },

    // ----------------------------------------------------------------------------------- 3D
    toggle3D() {
      this.d3 = !this.d3;
      this.map.set3D(this.d3);
      this.refreshLod2(true);
      this.saveHash();
    },
    /** LoD2 models of the buildings in view (3D, zoom >= 15); else the footprints are extruded. */
    async refreshLod2(recolour = false) {
      if (!this.map) return;
      const buildings = this.visible('lv_buildings');
      const wanted = this.d3 && this.lod2Available && buildings && this.zoom >= LOD2_ZOOM;
      if (!wanted) {
        this.lod2 = this.d3 && buildings ? { text: !this.lod2Available ? 'No LoD2 data in this database: buildings are extruded footprints'
          : `Zoom in (from zoom ${LOD2_ZOOM}) for LoD2 roofs` } : null;
        this.lod2Bbox = null;
        await showLod2(null);
        this.map.setLod2Hide(null);
        return;
      }
      if (!recolour && inside(this.map.bbox(0), this.lod2Bbox)) return;
      const box = this.lod2Bbox && recolour ? this.lod2Bbox : this.map.bbox(0.2).map((v) => Number(v.toFixed(4)));
      this.lod2Bbox = box;
      const scheme = schemeOf('lv_buildings', this.modeOf('lv_buildings'));
      const byId = new Map((this.map.data('lv_buildings')?.features || []).map((ft) => [ft.properties.fid, ft.properties]));
      this.lod2 = { text: 'Loading LoD2 buildings …' };
      try {
        const head = await showLod2({ db: this.db, version: this.version, bbox: box.join(',') },
          (oid) => (byId.has(oid) ? colourOf(scheme, byId.get(oid)) : null), document.documentElement.dataset.theme === 'dark');
        const n = head?.buildings?.length || 0;
        this.map.setLod2Hide(n ? head.buildings : null);
        this.lod2 = { text: n ? `${f.num(n)} buildings as LoD2 models${head.truncated ? ' (first 5,000)' : ''}` : 'No LoD2 models here: footprints are extruded' };
      } catch (err) {
        this.lod2 = { text: `LoD2: ${err.message}` };
      }
    },

    // ------------------------------------------------------------------------------- reload
    async refreshAll() {
      try {
        const list = await getJson('/api/databases', { refresh: 1 });
        this.databases = list.databases;
        if (!this.databases.includes(this.db)) {
          this.notify(`${this.db} is no longer on the server: back to ${list.default}`);
          this.setDatabase(list.default);
          return;
        }
        const catalog = await this.api('/api/catalog', { refresh: 1 });
        Object.assign(this, { meta: catalog.layers, groupMeta: catalog.groups, lod2Available: catalog.lod2,
          mvVariants: catalog.mv_variants, versions: await this.api('/api/versions') });
        for (const m of this.meta) {
          this.layers[m.id] ||= { on: m.default, mode: defaultMode(m.id), status: 'idle', count: null, note: '', truncated: false, byView: null, inView: false };
          this.map.setLayer(m.id, { attribution: m.attribution, minZoom: m.min_zoom });
        }
        if (!this.versions.some((v) => v.version_id === this.version)) this.version = this.versions[0]?.version_id || null;
        if (!this.runs.some((r) => r.id === this.mv)) this.mv = this.defaultRun();
        this.loaded = {};
        this.applyVisibility();
        this.reload(null, true);
        this.loadLists();
        this.loadSummary();
        this.notify('Reloaded from the database', 'info');
      } catch (err) {
        this.notify(err.message);
      }
    },

    // ------------------------------------------------------------------------------- search
    searchInput() {
      clearTimeout(this.searchTimer);
      const text = this.search.text;
      if (!text.trim()) { this.search.hits = []; return; }
      this.searchTimer = setTimeout(async () => {
        try {
          const db = this.db;
          const hits = await this.api('/api/search', { q: text, version: this.version, mv: this.mv });
          if (this.search.text === text && this.db === db) Object.assign(this.search, { hits, open: true, index: 0 });
        } catch (err) {
          this.notify(err.message);
        }
      }, 180);
    },
    searchKey(e) {
      const s = this.search;
      if (e.key === 'ArrowDown') { s.index = Math.min(s.index + 1, s.hits.length - 1); e.preventDefault(); }
      if (e.key === 'ArrowUp') { s.index = Math.max(s.index - 1, 0); e.preventDefault(); }
      if (e.key === 'Enter' && s.hits[s.index]) this.choose(s.hits[s.index]);
      if (e.key === 'Escape') { s.open = false; e.target.blur(); }
    },
    closeSearch() {
      setTimeout(() => { this.search.open = false; }, 150);
    },
    tooltipStyle() {
      const t = this.tooltip;
      return { left: `${Math.min(t.x + 16, window.innerWidth - 300)}px`, top: `${Math.min(t.y + 16, window.innerHeight - 170)}px` };
    },
    choose(hit) {
      Object.assign(this.search, { open: false, text: hit.label });
      if (hit.plz) this.selectRegion('plz', hit.plz);
      else if (hit.area) this.selectRegion('area', hit.area);
      else if (hit.grid) this.selectRegion('grid', hit.grid);
    },

    // ------------------------------------------------------------------------------ helpers
    key(e) {
      const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
      if (e.key === '/' && !typing) { e.preventDefault(); this.$refs.search?.focus(); }
      if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'Escape') { if (this.help) this.help = false; else if (this.inspector) this.closeInspector(); else if (this.region) this.clearRegion(); }
      if (e.key === '3') this.toggle3D();
      if (e.key === 'l') this.panel = !this.panel;
      if (e.key === '?') this.help = !this.help;
    },
    notify(text, kind = 'error') {
      this.toast = { text, kind };
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => { this.toast = null; }, 5000);
    },
    saveHash() {
      clearTimeout(this.hashTimer);
      this.hashTimer = setTimeout(() => {
        if (!this.map) return;
        const v = this.map.view();
        const groups = this.groupMeta.filter((g) => this.groups[g.id]?.on).map((g) => g.id);
        const on = this.meta.filter((m) => !m.parent && this.layers[m.id]?.on).map((m) => m.id);
        const modes = this.meta.filter((m) => this.layers[m.id]?.on && this.layers[m.id].mode && this.layers[m.id].mode !== defaultMode(m.id))
          .map((m) => `${m.id}:${this.layers[m.id].mode}`);
        const parts = { db: this.db, v: this.version, mv: this.mv, r: this.region ? `${this.region.kind}:${this.region.id}` : null,
          g: groups.join(','), l: on.join(','), m: modes.join(','), b: this.basemap !== 'streets' ? this.basemap : null, '3d': this.d3 ? null : '0',
          c: [v.center[0].toFixed(5), v.center[1].toFixed(5), v.zoom.toFixed(2), Math.round(v.pitch), Math.round(v.bearing)].join(',') };
        const hash = Object.entries(parts).filter(([k, x]) => x !== null && x !== undefined && (x !== '' || k === 'g' || k === 'l'))
          .map(([k, x]) => `${k}=${encodeURIComponent(x)}`).join('&');
        history.replaceState(null, '', `#${hash}`);
      }, 300);
    },
    attrValue: f.value,
  },
}).mount('#app');
