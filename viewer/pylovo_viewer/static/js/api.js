// Small fetch helpers for the viewer's API (same origin).

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export function url(path, params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') q.set(k, v);
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

async function request(path, params, opts = {}) {
  const res = await fetch(url(path, params), opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    throw new ApiError(detail, res.status);
  }
  return res;
}

export async function getJson(path, params, opts) {
  return (await request(path, params, opts)).json();
}
export async function getBuffer(path, params, opts) {
  return (await request(path, params, opts)).arrayBuffer();
}
