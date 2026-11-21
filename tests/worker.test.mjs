import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { parseRange, parseRoute } from '../worker/index.mjs';

class Bucket {
  data = new Map();
  object(key, range) {
    const value = this.data.get(key);
    if (!value) return null;
    const bytes = new TextEncoder().encode(value);
    return { size: bytes.length, httpEtag: '"test"', json: async () => JSON.parse(value),
      body: range ? bytes.slice(range.offset, range.offset + range.length) : bytes };
  }
  async get(key, options) { return this.object(key, options?.range); }
  async head(key) { return this.object(key); }
  async put(key, value) { this.data.set(key, value); }
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
