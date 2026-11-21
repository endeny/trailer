import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker/index.mjs';
const base='trailers/v1/movie/299534/en';
const digest='a'.repeat(64);
function setup(legacy=false) {
 const manifest={status:'ready',storage:'hf',hf_repo_id:'saltpi/iplay-trailers',hf_revision:'b'.repeat(40),variants:{}};
 for(const q of ['720p','1080p']) manifest.variants[q]={path:`${base}/${digest}/${q}.mp4`,sha256:digest,size_bytes:100,width:q==='720p'?1280:1920,height:q==='720p'?720:1080};
 const env={HF_REPO_ID:'saltpi/iplay-trailers',HF_TOKEN:'private-token',CDN_BASE_URL:'https://trailers.saltpi.cn',PUBLIC_MEDIA_ORIGIN:'https://media.example',
 TRAILERS:{get:async()=>({json:async()=>manifest}),head:async()=>legacy?{size:10}:null}};
 return {env,manifest};
}
const status='https://worker.example/v1/trailers/movie/299534/en';
const media='https://worker.example/media/movie/299534/en.mp4';
test('HF takes priority over an existing R2 video and exposes two stable quality URLs',async()=>{
 const {env}=setup(true);const state=await(await worker.fetch(new Request(status),env)).json();
 assert.equal(state.storage,'hf');assert.equal(state.trailer_url,'https://media.example/media/movie/299534/en.mp4?quality=720p');
 assert.ok(state.trailer_urls['1080p'].endsWith('quality=1080p'));assert.ok(!JSON.stringify(state).includes('private-token'));
});
test('HF redirect keeps the master credential server-side and does not cache signed URLs',async()=>{
 const {env}=setup();const original=globalThis.fetch;
 globalThis.fetch=async(url,opts)=>{assert.ok(url.endsWith('/1080p.mp4'));assert.equal(opts.redirect,'manual');assert.equal(opts.headers.Authorization,'Bearer private-token');
 return new Response(null,{status:302,headers:{Location:'https://cas-bridge.xethub.hf.co/file?signature=file-scoped'}});};
 try{const r=await worker.fetch(new Request(media+'?quality=1080p',{headers:{Range:'bytes=20-40'}}),env);assert.equal(r.status,302);assert.equal(r.headers.get('X-Trailer-Quality'),'1080p');assert.equal(r.headers.get('Cache-Control'),'no-store');assert.ok(!r.headers.get('Location').includes('private-token'));}finally{globalThis.fetch=original;}
});
test('malformed HF path/revision/repo never results in authenticated arbitrary fetch',async()=>{
 const {env,manifest}=setup();const original=globalThis.fetch;let calls=0;globalThis.fetch=async()=>{calls++;throw Error();};
 try{for(const field of ['hf_repo_id','hf_revision']) {const old=manifest[field];manifest[field]='evil';assert.equal((await(await worker.fetch(new Request(status),env)).json()).trailer_url,null);manifest[field]=old;}
 for(const v of Object.values(manifest.variants))v.path='../secret';assert.equal((await(await worker.fetch(new Request(status),env)).json()).trailer_url,null);assert.equal(calls,0);}finally{globalThis.fetch=original;}
});
test('HF failures fall back only to an existing legacy R2 video',async()=>{
 const original=globalThis.fetch;globalThis.fetch=async()=>new Response(null,{status:429});
 try{for(const legacy of [false,true]){const {env}=setup(legacy);const r=await worker.fetch(new Request(media),env);assert.equal(r.status,legacy?302:502);if(legacy)assert.equal(r.headers.get('Location'),`https://trailers.saltpi.cn/${base}/video.mp4`);}}finally{globalThis.fetch=original;}
});
test('unexpected redirect hosts are rejected without forwarding the token',async()=>{
 const {env}=setup();const original=globalThis.fetch;let calls=0;globalThis.fetch=async()=>{calls++;return new Response(null,{status:302,headers:{Location:'https://evil.example/leak'}});};
 try{assert.equal((await worker.fetch(new Request(media),env)).status,502);assert.equal(calls,1);}finally{globalThis.fetch=original;}
});
test('direct HF serving forwards the real Range rather than serving its one-byte probe',async()=>{
 const {env,manifest}=setup();delete manifest.variants['1080p'];const original=globalThis.fetch;let calls=0;
 globalThis.fetch=async(url,opts)=>{calls++;assert.equal(opts.headers.Range,calls===1?'bytes=0-0':'bytes=20-40');return new Response('abc',{status:206,headers:{'Content-Range':'bytes 20-22/100','Set-Cookie':'secret'}});};
 try{const r=await worker.fetch(new Request(media+'?quality=1080p',{headers:{Range:'bytes=20-40'}}),env);assert.equal(r.status,206);assert.equal(r.headers.get('X-Trailer-Quality'),'720p');assert.equal(r.headers.get('Set-Cookie'),null);assert.equal(await r.text(),'abc');assert.equal(calls,2);}finally{globalThis.fetch=original;}
});
test('missing HF configuration stops new dispatches without consuming daily budget',async()=>{
 const {env}=setup();env.HF_TOKEN='';env.STORAGE_BACKEND='hf';env.SUBMIT_TOKEN='submit';env.TRAILERS.get=async()=>null;
 const r=await worker.fetch(new Request(status,{method:'POST',headers:{Authorization:'Bearer submit'}}),env);
 assert.equal(r.status,503);assert.equal((await r.json()).error,'hf_not_configured');
});
