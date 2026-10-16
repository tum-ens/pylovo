// LoD2 building models of the selected region (citydb), drawn as a MapLibre custom 3D layer.
//
// Adapted from the GridPlanner map (ui/static/js/panels/map_lod2.js). The mesh format is
// pylovo-api's (api/pylovo_api/lod2.py): positions in EPSG:3857 metres relative to `origin`, z in
// metres above the building's ground. Roofs take the building's map colour, walls a light mix of it.
// Buildings without LoD2 stay extruded footprints; a database without citydb gives an empty mesh.
import * as maplibregl from 'maplibre-gl';
import { getBuffer } from './api.js';

const EARTH_CIRCUMFERENCE = 2 * Math.PI * 6378137;
const LIGHT = (() => { const v = [-0.45, -0.55, 0.7]; const n = Math.hypot(...v); return v.map((x) => x / n); })();

const cache = new Map();   // region key -> parsed mesh (a few regions)
let current = null;        // mesh on screen: { key, header, pos, idx, bld, cls, colors }
let layerRef = null;

export function parse(buffer) {
  const view = new DataView(buffer);
  if (String.fromCharCode(...new Uint8Array(buffer, 0, 4)) !== 'LOD2') throw new Error('not a LoD2 mesh');
  const headLen = view.getUint32(8, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 12, headLen)));
  const nv = header.vertices || 0;
  const nt = header.triangles || 0;
  if (!nv || !nt) return { header };
  let o = 12 + headLen;
  const pos = new Float32Array(buffer, o, nv * 3); o += nv * 12;
  const idx = new Uint32Array(buffer, o, nt * 3); o += nt * 12;
  const wide = header.building_index_bytes === 4;
  const bld = wide ? new Uint32Array(buffer, o, nv) : new Uint16Array(buffer, o, nv); o += nv * (wide ? 4 : 2);
  const cls = new Uint8Array(buffer, o, nv);
  return { header, pos, idx, bld, cls };
}

const colorCtx = document.createElement('canvas').getContext('2d');
const rgbCache = new Map();
function rgb(css) {
  if (rgbCache.has(css)) return rgbCache.get(css);
  colorCtx.fillStyle = '#969696';
  colorCtx.fillStyle = css;
  const v = colorCtx.fillStyle;
  const out = v.startsWith('#') ? [1, 3, 5].map((i) => parseInt(v.slice(i, i + 2), 16)) : (v.match(/[\d.]+/g) || [150, 150, 150]).slice(0, 3).map(Number);
  rgbCache.set(css, out);
  return out;
}

function vertexColors(mesh, colourOf, dark) {
  const n = mesh.cls.length;
  const out = new Uint8Array(n * 4);
  const wallBase = dark ? [71, 85, 105] : [226, 229, 234];
  const perBuilding = mesh.header.buildings.map((objectid) => {
    const roof = rgb(colourOf(objectid) || '#9ca3af');
    return [roof, roof.map((c, i) => Math.round(c * 0.3 + wallBase[i] * 0.7))];
  });
  for (let i = 0; i < n; i++) {
    const [roof, wall] = perBuilding[mesh.bld[i]] || [[150, 150, 150], wallBase];
    const c = mesh.cls[i] === 1 ? roof : wall;
    out[i * 4] = c[0]; out[i * 4 + 1] = c[1]; out[i * 4 + 2] = c[2]; out[i * 4 + 3] = 255;
  }
  return out;
}

const VERTEX = `#version 300 es
uniform mat4 u_matrix;
uniform float u_cos;
in vec3 a_pos;
in vec4 a_col;
out vec3 v_pos;
out vec4 v_col;
void main() {
  gl_Position = u_matrix * vec4(a_pos, 1.0);
  v_pos = vec3(a_pos.xy * u_cos, a_pos.z);
  v_col = a_col;
}`;
const FRAGMENT = `#version 300 es
precision highp float;
uniform vec3 u_light;
in vec3 v_pos;
in vec4 v_col;
out vec4 fragColor;
void main() {
  vec3 n = normalize(cross(dFdx(v_pos), dFdy(v_pos)));
  float shade = 0.6 + 0.4 * abs(dot(n, u_light));
  fragColor = vec4(v_col.rgb * shade * v_col.a, v_col.a);
}`;

function compile(gl, type, source) {
  const s = gl.createShader(type);
  gl.shaderSource(s, source);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'shader');
  return s;
}

function multiply(a, b) {   // column-major 4x4: a · b
  const out = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      out[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
  }
  return out;
}

/** A new custom layer; it draws whatever mesh is current (a style reload re-adds it). */
export function createLod2Layer(id = 'lod2') {
  let gl = null; let prog = null; let vao = null; let buffers = null; let uploaded = null; let map = null;
  let colorsDirty = false;
  const layer = {
    id, type: 'custom', renderingMode: '3d',
    onAdd(m, context) {
      map = m; gl = context;
      try {
        prog = gl.createProgram();
        gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERTEX));
        gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT));
        gl.linkProgram(prog);
        if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) || 'link');
      } catch (err) {
        console.warn('LoD2 layer disabled (needs WebGL2):', err);
        prog = null;
      }
      layerRef = layer;
    },
    onRemove() {
      if (gl && buffers) Object.values(buffers).forEach((b) => gl.deleteBuffer(b));
      if (gl && vao) gl.deleteVertexArray(vao);
      if (gl && prog) gl.deleteProgram(prog);
      buffers = null; vao = null; prog = null; uploaded = null;
      if (layerRef === layer) layerRef = null;
    },
    upload() {
      if (!gl || !prog || !current?.pos) { uploaded = null; return; }
      if (!vao) {
        vao = gl.createVertexArray();
        buffers = { pos: gl.createBuffer(), col: gl.createBuffer(), idx: gl.createBuffer() };
      }
      gl.bindVertexArray(vao);
      const aPos = gl.getAttribLocation(prog, 'a_pos');
      const aCol = gl.getAttribLocation(prog, 'a_col');
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.pos);
      gl.bufferData(gl.ARRAY_BUFFER, current.pos, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.col);
      gl.bufferData(gl.ARRAY_BUFFER, current.colors, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(aCol);
      gl.vertexAttribPointer(aCol, 4, gl.UNSIGNED_BYTE, true, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, buffers.idx);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, current.idx, gl.STATIC_DRAW);
      gl.bindVertexArray(null);
      const [lon, lat] = current.header.origin;
      const o = maplibregl.MercatorCoordinate.fromLngLat({ lng: lon, lat }, 0);
      const s = 1 / EARTH_CIRCUMFERENCE;
      uploaded = {
        mesh: current, count: current.idx.length, cos: Math.cos((lat * Math.PI) / 180),
        model: [s, 0, 0, 0, 0, -s, 0, 0, 0, 0, o.meterInMercatorCoordinateUnits(), 0, o.x, o.y, 0, 1],
      };
    },
    markColors() { colorsDirty = true; },
    render(context, args) {
      if (!prog) return;
      if (current?.pos && (!uploaded || uploaded.mesh !== current)) { this.upload(); colorsDirty = false; }
      if (!uploaded || !current?.pos) return;
      if (colorsDirty) {
        gl.bindBuffer(gl.ARRAY_BUFFER, buffers.col);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, current.colors);
        colorsDirty = false;
      }
      const view = args?.defaultProjectionData?.mainMatrix || args?.modelViewProjectionMatrix || args;
      gl.useProgram(prog);
      gl.bindVertexArray(vao);
      gl.uniformMatrix4fv(gl.getUniformLocation(prog, 'u_matrix'), false, multiply(view, uploaded.model));
      gl.uniform1f(gl.getUniformLocation(prog, 'u_cos'), uploaded.cos);
      gl.uniform3fv(gl.getUniformLocation(prog, 'u_light'), LIGHT);
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.depthMask(true);
      gl.disable(gl.CULL_FACE);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawElements(gl.TRIANGLES, uploaded.count, gl.UNSIGNED_INT, 0);
      gl.bindVertexArray(null);
    },
    repaint() { map?.triggerRepaint(); },
  };
  return layer;
}

/**
 * Show the LoD2 mesh of a region (`params` = version + plz|area|grid), or nothing for `null`.
 * Resolves to the header ({available, requested, with_lod2, buildings, truncated, reason}).
 */
export async function showLod2(params, colourOf, dark) {
  if (!params) {
    current = null;
    layerRef?.repaint();
    return null;
  }
  const key = JSON.stringify(params);
  if (current?.key === key) { recolorLod2(colourOf, dark); return current.header; }
  let mesh = cache.get(key);
  if (!mesh) {
    mesh = parse(await getBuffer('/api/lod2', params));
    cache.set(key, mesh);
    while (cache.size > 4) cache.delete(cache.keys().next().value);
  }
  current = mesh.pos ? { ...mesh, key, colors: vertexColors(mesh, colourOf, dark) } : null;
  layerRef?.repaint();
  return mesh.header;
}

export function recolorLod2(colourOf, dark) {
  if (!current?.pos) return;
  current.colors = vertexColors(current, colourOf, dark);
  layerRef?.markColors();
  layerRef?.repaint();
}

/** Object ids drawn as LoD2 right now (their footprints are hidden). */
export function lod2Buildings() {
  return current?.pos ? current.header.buildings : [];
}
