// HF credentials stay on the Worker; clients receive only short-lived file URLs.
export function hfManifest(env, route, state) {
  if (state.status !== 'ready' || state.storage !== 'hf' || state.hf_repo_id !== env.HF_REPO_ID ||
      !/^[a-f0-9]{40,64}$/.test(state.hf_revision || '')) return null;
  const variants = {};
  for (const quality of ['720p', '1080p']) {
    const v = state.variants?.[quality];
    if (v && /^[a-f0-9]{64}$/.test(v.sha256 || '') &&
        v.path === `${route.base}/${v.sha256}/${quality}.mp4` &&
        Number.isSafeInteger(v.size_bytes) && v.size_bytes > 0 && v.size_bytes <= 120 * 1024 * 1024 &&
        Number.isSafeInteger(v.width) && Number.isSafeInteger(v.height) && v.width > 0 && v.height > 0 &&
        v.width <= (quality === '720p' ? 1280 : 1920) && v.height <= (quality === '720p' ? 720 : 1080)) variants[quality] = v;
  }
  return Object.keys(variants).length ? { ...state, variants } : null;
}

export function hfStatus(env, route, state, origin) {
  const manifest = hfManifest(env, route, state);
  if (!manifest) return null;
  const base = new URL(env.PUBLIC_MEDIA_ORIGIN || origin);
  if (base.protocol !== 'https:' || base.pathname !== '/' || base.search || base.hash) throw Error('invalid_media_origin');
  const media = `${base.origin}/media/${route.type}/${route.id}/${route.language}.mp4`;
  const qualities = Object.keys(manifest.variants);
  const quality = qualities.includes(env.DEFAULT_TRAILER_QUALITY || '720p') ? env.DEFAULT_TRAILER_QUALITY || '720p' : qualities[0];
  return { media_type: route.type, tmdb_id: Number(route.id), language: route.language, status: 'ready', storage: 'hf',
    trailer_url: `${media}?quality=${quality}`, quality, size_bytes: manifest.variants[quality].size_bytes,
    trailer_urls: Object.fromEntries(qualities.map(q => [q, `${media}?quality=${q}`])) };
}

export async function hfMedia(request, env, route, manifest) {
  if (!env.HF_TOKEN) throw Error('hf_not_configured');
  const requested = new URL(request.url).searchParams.get('quality') || env.DEFAULT_TRAILER_QUALITY || '720p';
  const quality = manifest.variants[requested] ? requested : (manifest.variants['720p'] ? '720p' : Object.keys(manifest.variants)[0]);
  const variant = manifest.variants[quality];
  const url = `https://huggingface.co/datasets/${env.HF_REPO_ID}/resolve/${manifest.hf_revision}/${variant.path}`;
  const options = { redirect: 'manual', signal: AbortSignal.timeout(15000),
    headers: { Authorization: `Bearer ${env.HF_TOKEN}`, Range: 'bytes=0-0' } };
  const probe = await fetch(url, options);
  if ([301,302,303,307,308].includes(probe.status)) {
    const location = new URL(probe.headers.get('Location'), url);
    if (location.protocol !== 'https:' || location.username || location.password ||
        !(location.hostname === 'huggingface.co' || location.hostname.endsWith('.huggingface.co') || location.hostname.endsWith('.hf.co'))) {
      await probe.body?.cancel(); throw Error('invalid_hf_redirect');
    }
    await probe.body?.cancel();
    return new Response(null, { status: 302, headers: { Location: location.href, 'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer', 'X-Trailer-Storage': 'hf', 'X-Trailer-Quality': quality } });
  }
  await probe.body?.cancel();
  if (!probe.ok) throw Error('hf_unavailable');
  // Small non-LFS files can be served directly. Repeat with the caller's actual range.
  const headers = { Authorization: `Bearer ${env.HF_TOKEN}` };
  for (const name of ['Range','If-Range','If-None-Match']) if (request.headers.has(name)) headers[name] = request.headers.get(name);
  const response = await fetch(url, { ...options, method: request.method, headers });
  if (![200,206,304,416].includes(response.status)) { await response.body?.cancel(); throw Error('hf_unavailable'); }
  const output = new Headers({ 'Content-Type': 'video/mp4', 'Cache-Control': 'no-store', 'X-Trailer-Storage': 'hf', 'X-Trailer-Quality': quality });
  for (const name of ['Content-Length','Content-Range','Accept-Ranges','ETag','Last-Modified']) {
    if (response.headers.has(name)) output.set(name, response.headers.get(name));
  }
  return new Response(request.method === 'HEAD' ? null : response.body, { status: response.status, headers: output });
}
