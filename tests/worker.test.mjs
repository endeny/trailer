import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { parseRange, parseRoute, reserveDailySlot } from '../worker/index.mjs';

class Bucket {
  data = new Map();
  revisions = new Map();
  object(key, range) {
    const value = this.data.get(key);
    if (!value) return null;
    const bytes = new TextEncoder().encode(value);
    const etag = key.endsWith("video.mp4") ? "test" : String(this.revisions.get(key) || 0);
    return { size: bytes.length, etag, httpEtag: `"${etag}"`, json: async () => JSON.parse(value),
      body: range ? bytes.slice(range.offset, range.offset + range.length) : bytes };
  }
  async get(key, options) { return this.object(key, options?.range); }
  async head(key) { return this.object(key); }
  async put(key, value, options) {
    const current = this.object(key);
    if (options?.onlyIf?.etagDoesNotMatch === "*" && current) return null;
    if (options?.onlyIf?.etagMatches && options.onlyIf.etagMatches !== current?.etag) return null;
    this.data.set(key, value); this.revisions.set(key, (this.revisions.get(key) || 0) + 1);
    return this.object(key);
  }
}
function env() { return { TRAILERS: new Bucket(), SUBMIT_TOKEN: 'submit', GITHUB_TOKEN: 'github', GITHUB_REPOSITORY: 'endeny/trailer' }; }
const base = 'trailers/v1/movie/299534/en';
const url = 'https://trailers.example/v1/trailers/movie/299534/en';
const post = () => new Request(url, { method: 'POST', headers: { Authorization: 'Bearer submit' } });

test('only bounded canonical media IDs/languages are accepted', () => {
  assert.equal(parseRoute('/v1/trailers/movie/12/en').id, '12');
  assert.equal(parseRoute('/media/tv/12/de.mp4').media, true);
  for (const path of ['/v1/trailers/movie/0/en', '/media/tv/12/en', '/media/movie/12/en.mp4/evil', '/v1/trailers/movie/12/en.mp4']) assert.equal(parseRoute(path), null);
});
test('ranges include suffix/open-ended requests and reject invalid/multipart ranges', () => {
  assert.deepEqual(parseRange('bytes=2-5', 10), { offset: 2, length: 4 });
  assert.deepEqual(parseRange('bytes=-4', 10), { offset: 6, length: 4 });
  assert.deepEqual(parseRange('bytes=7-', 10), { offset: 7, length: 3 });
  for (const value of ['bytes=10-', 'bytes=7-2', 'bytes=-0', 'bytes=0-1,4-5', 'x']) assert.equal(parseRange(value, 10), null);
});
test('POST requires a configured secret and never dispatches on GET', async () => {
  const e = env();
  assert.equal((await worker.fetch(new Request(url, { method: 'POST' }), e)).status, 401);
  const status = await worker.fetch(new Request(url), e);
  assert.equal((await status.json()).status, 'missing');
  assert.equal(e.TRAILERS.data.size, 0);
});
test('dispatch writes pending first; a running Action is never overwritten', async () => {
  const e = env(); const original = globalThis.fetch; let body;
  globalThis.fetch = async (_url, options) => {
    assert.equal(JSON.parse(e.TRAILERS.data.get(`${base}/metadata.json`)).status, 'pending');
    body = JSON.parse(options.body);
    await e.TRAILERS.put(`${base}/metadata.json`, JSON.stringify({ status: 'running', updated_at: new Date().toISOString() }));
    return new Response(null, { status: 204 });
  };
  try {
    assert.equal((await worker.fetch(post(), e)).status, 202);
    assert.deepEqual(body.inputs, { media_type: 'movie', tmdb_id: '299534', language: 'en' });
    assert.equal(JSON.parse(e.TRAILERS.data.get(`${base}/metadata.json`)).status, 'running');
    assert.equal((await worker.fetch(post(), e)).status, 202);
  } finally { globalThis.fetch = original; }
});
test('rejected dispatch produces retriable failure; response does not leak GitHub details', async () => {
  const e = env(); const original = globalThis.fetch;
  globalThis.fetch = async () => new Response('secret diagnostic', { status: 403 });
  try {
    assert.equal((await worker.fetch(post(), e)).status, 502);
    const state = JSON.parse(e.TRAILERS.data.get(`${base}/metadata.json`));
    assert.equal(state.error, 'dispatch_failed');
    assert.equal((await worker.fetch(post(), e)).status, 429);
  } finally { globalThis.fetch = original; }
});
test('stale jobs expire; ready metadata alone cannot claim a video exists', async () => {
  const e = env();
  await e.TRAILERS.put(`${base}/metadata.json`, JSON.stringify({ status: 'running', updated_at: '2020-01-01T00:00:00Z' }));
  assert.equal((await (await worker.fetch(new Request(url), e)).json()).status, 'expired');
  await e.TRAILERS.put(`${base}/metadata.json`, JSON.stringify({ status: 'ready' }));
  assert.equal((await (await worker.fetch(new Request(url), e)).json()).status, 'missing');
});
test('cached objects win over stale states, without returning credentials or internal errors', async () => {
  const e = env();
  await e.TRAILERS.put(`${base}/metadata.json`, JSON.stringify({ status: 'failed', error: 'old' }));
  await e.TRAILERS.put(`${base}/video.mp4`, '0123456789');
  const response = await worker.fetch(post(), e);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).trailer_url, 'https://trailers.example/media/movie/299534/en.mp4');
});
test('native player GET, HEAD, Range and conditional requests', async () => {
  const e = env(); const media = 'https://trailers.example/media/movie/299534/en.mp4';
  await e.TRAILERS.put(`${base}/video.mp4`, '0123456789');
  let r = await worker.fetch(new Request(media, { headers: { Range: 'bytes=2-5' } }), e);
  assert.equal(r.status, 206); assert.equal(r.headers.get('Content-Range'), 'bytes 2-5/10'); assert.equal(await r.text(), '2345');
  r = await worker.fetch(new Request(media, { method: 'HEAD' }), e);
  assert.equal(r.headers.get('Content-Length'), '10'); assert.equal(await r.text(), '');
  r = await worker.fetch(new Request(media, { headers: { 'If-None-Match': '"test"' } }), e);
  assert.equal(r.status, 304);
  r = await worker.fetch(new Request(media, { headers: { Range: 'bytes=10-' } }), e);
  assert.equal(r.status, 416); assert.equal(r.headers.get('Content-Range'), 'bytes */10');
  r = await worker.fetch(new Request(media, { headers: { Range: 'bytes=2-5', 'If-Range': '"old"' } }), e);
  assert.equal(r.status, 200); assert.equal(await r.text(), '0123456789');
});


test('CDN media redirects do not read R2 and ready data returns the direct CDN URL', async () => {
  const e = env(); e.CDN_BASE_URL = 'https://trailers.saltpi.cn';
  const r = await worker.fetch(new Request('https://worker.example/media/movie/299534/en.mp4'), e);
  assert.equal(r.status, 302);
  assert.equal(r.headers.get('Location'), 'https://trailers.saltpi.cn/trailers/v1/movie/299534/en/video.mp4');
  assert.equal(e.TRAILERS.data.size, 0);
  await e.TRAILERS.put(`${base}/video.mp4`, 'video');
  const state = await (await worker.fetch(new Request(url), e)).json();
  assert.equal(state.trailer_url, r.headers.get('Location'));
});
test('concurrent daily reservations cannot exceed the configured budget', async () => {
  const e = env(); e.DAILY_DOWNLOAD_LIMIT = '2';
  const accepted = await Promise.all(Array.from({length: 6}, () => reserveDailySlot(e)));
  assert.equal(accepted.filter(Boolean).length, 2);
});
test('simultaneous submissions only dispatch one job for an identity', async () => {
  const e = env(); const original = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response(null, {status: 204}); };
  try {
    const responses = await Promise.all([worker.fetch(post(), e), worker.fetch(post(), e)]);
    assert.ok(responses.every(r => r.status === 202)); assert.equal(calls, 1);
  } finally { globalThis.fetch = original; }
});
test('internal batch is authenticated, bounded, and returns movie/tv identities separately', async () => {
  const e = env(); await e.TRAILERS.put(`${base}/video.mp4`, 'video');
  const request = (items, token = 'submit') => new Request('https://worker.example/internal/resolve', {method:'POST', headers:{Authorization:`Bearer ${token}`}, body:JSON.stringify({items})});
  assert.equal((await worker.fetch(request([], 'wrong'), e)).status, 401);
  assert.equal((await worker.fetch(request(Array(7).fill({type:'movie',id:1})), e)).status, 400);
  const r = await worker.fetch(request([{type:'movie',id:299534},{type:'tv',id:299534}]),e);
  const data = await r.json(); assert.equal(data.results[0].status,'ready'); assert.equal(data.results[1].status,'missing');
});
