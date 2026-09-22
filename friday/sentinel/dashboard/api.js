/* fetch wrapper: same-origin cookies, the CSRF header, JSON bodies, NDJSON streams.
   Paths are relative ("api/settings") so a reverse-proxy prefix keeps working. */

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function options(opts) {
  const out = {
    credentials: "same-origin",
    method: opts.method || "GET",
    headers: Object.assign({ "X-FRIDAY-Client": "dashboard" }, opts.headers || {}),
    signal: opts.signal,
  };
  if (opts.json !== undefined) {
    out.body = JSON.stringify(opts.json);
    out.headers["Content-Type"] = "application/json";
  }
  return out;
}

function unauthorized(path) {
  if (path !== "auth/login") window.dispatchEvent(new CustomEvent("friday:unauthorized"));
  return new ApiError("signed out", 401, null);
}

async function parse(resp) {
  const text = await resp.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch (e) { return text; }
}

export async function api(path, opts = {}) {
  const resp = await fetch(path, options(opts));
  if (resp.status === 401) throw unauthorized(path);
  const body = await parse(resp);
  if (!resp.ok) throw new ApiError((body && body.error) || `HTTP ${resp.status}`, resp.status, body);
  return body;
}

export async function* stream(path, opts = {}) {
  const resp = await fetch(path, options(opts));
  if (resp.status === 401) throw unauthorized(path);
  if (!resp.ok) {
    const body = await parse(resp);
    throw new ApiError((body && body.error) || `HTTP ${resp.status}`, resp.status, body);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer.trim());
}
