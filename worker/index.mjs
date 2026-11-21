const PREFIX = "trailers/v1";
const JOB_TTL_MS = 30 * 60 * 1000;
const RETRY_MS = 6 * 60 * 60 * 1000;
const STATE_ETAG = Symbol("stateEtag");

export function parseRoute(path) {
  const match = path.match(/^\/(v1\/trailers|media)\/(movie|tv)\/([1-9]\d{0,9})\/([a-z]{2})(\.mp4)?$/);
  if (!match || (match[1] === "media") !== Boolean(match[5])) return null;
  return { media: match[1] === "media", type: match[2], id: match[3], language: match[4],
    base: `${PREFIX}/${match[2]}/${match[3]}/${match[4]}` };
}

function json(body, status = 200, extra = {}) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store", ...extra } });
}

async function authenticated(request, token) {
  if (!token) return false;
  const hash = async (value) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
  const [a, b] = await Promise.all([hash(request.headers.get("Authorization") || ""), hash(`Bearer ${token}`)]);
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

export function parseRange(header, size) {
  const match = header?.match(/^bytes=(\d*)-(\d*)$/);
  if (!match || (!match[1] && !match[2]) || !size) return null;
  let start, end;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    start = Math.max(0, size - suffix); end = size - 1;
  } else {
    start = Number(match[1]); end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  }
  if (![start, end].every(Number.isSafeInteger) || start >= size || end < start) return null;
  return { offset: start, length: end - start + 1 };
}

export function trailerURL(env, route, origin) {
  if (env.CDN_BASE_URL) {
    const cdn = new URL(env.CDN_BASE_URL);
    if (cdn.protocol !== "https:" || cdn.pathname !== "/" || cdn.search || cdn.hash) throw new Error("invalid_cdn_origin");
    return `${cdn.origin}/${route.base}/video.mp4`;
  }
  return `${origin}/media/${route.type}/${route.id}/${route.language}.mp4`;
}

async function mediaResponse(request, env, route) {
  if (env.CDN_BASE_URL) {
    return new Response(null, { status: 302, headers: {
      Location: trailerURL(env, route, new URL(request.url).origin),
      "Cache-Control": "public, max-age=86400"
    } });
  }
  const key = `${route.base}/video.mp4`;
  const object = await env.TRAILERS.head(key);
  if (!object) return json({ error: "not_cached" }, 404);
  const headers = new Headers({ "Content-Type": "video/mp4", "Accept-Ranges": "bytes",
    "ETag": object.httpEtag, "Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff" });
  if (request.headers.get("If-None-Match")?.split(/\s*,\s*/).some(v => v === object.httpEtag || v === "*")) {
    return new Response(null, { status: 304, headers });
  }
  // HEAD returns complete object headers; a matching If-Range permits partial GET.
  const rangeHeader = request.method === "GET" && (!request.headers.has("If-Range") || request.headers.get("If-Range") === object.httpEtag)
    ? request.headers.get("Range") : null;
  const range = rangeHeader ? parseRange(rangeHeader, object.size) : undefined;
  if (rangeHeader && !range) {
    headers.set("Content-Range", `bytes */${object.size}`);
    return new Response(null, { status: 416, headers });
  }
  headers.set("Content-Length", String(range ? range.length : object.size));
  if (range) headers.set("Content-Range", `bytes ${range.offset}-${range.offset + range.length - 1}/${object.size}`);
  if (request.method === "HEAD") return new Response(null, { headers });
  const body = await env.TRAILERS.get(key, range ? { range } : undefined);
  if (!body) return json({ error: "not_cached" }, 404);
  // An overwrite between head/get must not serve bytes under stale lengths/ETags.
  if (body.httpEtag !== object.httpEtag) return json({ error: "object_changed", retry_after: 1 }, 409);
  return new Response(body.body, { status: range ? 206 : 200, headers });
}

async function readState(env, route) {
  const object = await env.TRAILERS.get(`${route.base}/metadata.json`);
  if (!object) return { status: "missing" };
  const state = await object.json();
  state[STATE_ETAG] = object.etag;
  if (["pending", "running"].includes(state.status) && Date.now() - Date.parse(state.updated_at) > JOB_TTL_MS) {
    return { ...state, status: "expired", error: "job_timeout" };
  }
  return state;
}

async function saveState(env, route, state, onlyIf) {
  return env.TRAILERS.put(`${route.base}/metadata.json`, JSON.stringify(state), {
    httpMetadata: { contentType: "application/json", cacheControl: "no-store" },
    ...(onlyIf ? { onlyIf } : {})
  });
}

export async function reserveDailySlot(env) {
  // Atomic R2 CAS: no paid Durable Object, KV or queue is needed for this small daily budget.
  const day = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  const limit = Math.max(0, Math.min(32, Number(env.DAILY_DOWNLOAD_LIMIT ?? 8) || 0));
  const key = `trailer-control/v1/daily/${day}.json`;
  for (let attempt = 0; attempt < 5; attempt++) {
    const current = await env.TRAILERS.get(key);
    const count = current ? Number((await current.json()).count) : 0;
    if (!Number.isSafeInteger(count) || count < 0 || count >= limit) return false;
    const saved = await env.TRAILERS.put(key, JSON.stringify({ day, count: count + 1 }), {
      onlyIf: current ? { etagMatches: current.etag } : { etagDoesNotMatch: "*" },
      httpMetadata: { contentType: "application/json", cacheControl: "no-store" }
    });
    if (saved) return true;
  }
  return false;
}

async function internalResolve(request, env, ctx) {
  if (!await authenticated(request, env.SUBMIT_TOKEN)) return json({ error: "unauthorized" }, 401);
  if (Number(request.headers.get("Content-Length")) > 8192) return json({ error: "batch_too_large" }, 413);
  let input;
  try { input = await request.json(); } catch { return json({ error: "invalid_json" }, 400); }
  if (!Array.isArray(input.items) || input.items.length > 6 || input.items.some(item => !item || typeof item !== "object")) return json({ error: "invalid_batch" }, 400);
  const routes = input.items.map(item => parseRoute(`/v1/trailers/${item.type}/${item.id}/${item.language || "en"}`));
  if (routes.some(route => !route || route.media)) return json({ error: "invalid_identity" }, 400);
  const origin = new URL(request.url).origin;
  const results = await Promise.all(routes.map(async route => {
    const response = await handle(new Request(`${origin}/v1/trailers/${route.type}/${route.id}/${route.language}`), env, ctx);
    return { type: route.type, id: Number(route.id), language: route.language, ...(await response.json()) };
  }));
  let scheduled = 0;
  for (let i = 0; i < results.length; i++) {
    if (input.items[i].queue === true && !results[i].trailer_url && scheduled < 2) {
      scheduled++;
      const route = routes[i];
      const promise = handle(new Request(`${origin}/v1/trailers/${route.type}/${route.id}/${route.language}`, {
        method: "POST", headers: { Authorization: `Bearer ${env.SUBMIT_TOKEN}` }
      }), env, ctx).catch(() => {});
      if (ctx?.waitUntil) ctx.waitUntil(promise); else await promise;
    }
  }
  return json({ results });
}

async function handle(request, env, ctx) {
  const url = new URL(request.url);
  if (url.pathname === "/health" && request.method === "GET") return json({ ok: true });
  if (url.pathname === "/internal/resolve" && request.method === "POST") return internalResolve(request, env, ctx);
  const route = parseRoute(url.pathname);
  if (!route) return json({ error: "not_found" }, 404);
  const allowed = route.media ? ["GET", "HEAD"] : ["GET", "POST"];
  if (!allowed.includes(request.method)) return json({ error: "method_not_allowed" }, 405, { Allow: allowed.join(", ") });
  if (request.method === "POST" && !await authenticated(request, env.SUBMIT_TOKEN)) return json({ error: "unauthorized" }, 401);
  if (route.media) return mediaResponse(request, env, route);
  // Presence of the actual object is authoritative, including a completed upload whose final status write failed.
  const video = await env.TRAILERS.head(`${route.base}/video.mp4`);
  if (video) return json({ media_type: route.type, tmdb_id: Number(route.id), language: route.language,
    status: "ready", size_bytes: video.size,
    trailer_url: trailerURL(env, route, url.origin) });
  const state = await readState(env, route);
  if (request.method === "GET") return json({ ...state, trailer_url: null, status: state.status === "ready" ? "missing" : state.status });
  if (["pending", "running"].includes(state.status)) return json({ status: state.status, trailer_url: null }, 202, { "Retry-After": "15" });
  const retryMS = state.status === "not_found" ? 7 * 86400000 : RETRY_MS;
  if (["failed", "not_found"].includes(state.status) && Date.now() - Date.parse(state.updated_at) < retryMS) {
    const seconds = Math.ceil((retryMS - (Date.now() - Date.parse(state.updated_at))) / 1000);
    return json({ status: state.status, error: state.error, retry_after: seconds }, 429, { "Retry-After": String(seconds) });
  }
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(env.GITHUB_REPOSITORY || "") || env.GITHUB_REPOSITORY === "OWNER/REPOSITORY" || !env.GITHUB_TOKEN) {
    return json({ error: "dispatch_not_configured" }, 503);
  }
  const pending = { status: "pending", media_type: route.type, tmdb_id: Number(route.id), language: route.language,
    request_id: crypto.randomUUID(), updated_at: new Date().toISOString() };
  // Write before dispatch, so a fast Action cannot be overwritten by a later pending write.
  const claimed = await saveState(env, route, pending,
    state[STATE_ETAG] ? { etagMatches: state[STATE_ETAG] } : { etagDoesNotMatch: "*" });
  if (!claimed) return json({ status: "pending", trailer_url: null }, 202, { "Retry-After": "15" });
  if (!await reserveDailySlot(env)) {
    await saveState(env, route, { ...pending, status: "failed", error: "daily_download_budget_exceeded" });
    return json({ error: "daily_download_budget_exceeded" }, 429);
  }
  try {
    const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/${encodeURIComponent(env.GITHUB_WORKFLOW || "cache-trailer.yml")}/dispatches`, {
      method: "POST", signal: AbortSignal.timeout(15000),
      headers: { "Authorization": `Bearer ${env.GITHUB_TOKEN}`, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "iPlay-Trailer-Cache", "Content-Type": "application/json" },
      body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs: { media_type: route.type, tmdb_id: route.id, language: route.language } })
    });
    if (!response.ok) throw new Error("dispatch_failed");
  } catch {
    // Timeout may mean GitHub accepted the job. Preserve a state already updated by the Action.
    const latest = await readState(env, route);
    if (latest.request_id === pending.request_id && latest.status === "pending") {
      await saveState(env, route, { ...pending, status: "failed", error: "dispatch_failed", updated_at: new Date().toISOString() });
    }
    return json({ error: "dispatch_failed" }, 502);
  }
  return json({ status: "pending", trailer_url: null }, 202, { "Retry-After": "15" });
}

export default {
  async fetch(request, env, ctx) {
    try { return await handle(request, env, ctx); }
    catch { return json({ error: "storage_or_service_unavailable" }, 503); }
  }
};
