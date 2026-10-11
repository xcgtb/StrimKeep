const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const runtime=fs.readFileSync(path.join(__dirname,'../../static/js/runtime.js'),'utf8');
const pipeline=runtime.slice(runtime.indexOf('var POSTER_MAX_CONCURRENCY = 6;'),runtime.indexOf('function clearPosterLoading(img){'));
const loading=runtime.slice(runtime.indexOf('var __posterQueue ='),runtime.indexOf('function renderMenu(){'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){for(let i=0;i<8;i++)await tick();}
function env(extra={}){
  const ctx=vm.createContext({Map,Set,Promise,Object,Date,console,AbortController,Headers,Response,Blob,
    setTimeout,clearTimeout,window:{innerWidth:390,devicePixelRatio:3,innerHeight:800},
    clearPosterLoading(){},posterImageError(){},...extra});
  vm.runInContext(pipeline+loading,ctx);return ctx;
}
const image=()=>new Response(new Blob(['poster'],{type:'image/jpeg'}),{headers:{'Content-Type':'image/jpeg'}});
(async()=>{
  const pending=[];let active=0,maxActive=0;
  const ctx=env({fetch:url=>{active++;maxActive=Math.max(active,maxActive);return new Promise(resolve=>pending.push({url,done(){active--;resolve(image());}}));}});
  const tasks=[];
  for(let i=0;i<12;i++)tasks.push(ctx.posterFetch('/api/tmdb/poster/visible'+i+'.jpg'));
  for(let i=0;i<4;i++)tasks.push(ctx.posterFetch('/api/tmdb/poster/warm'+i+'.jpg',{prefetch:true,active:()=>true}));
  await settle();assert.equal(pending.length,6);assert.equal(maxActive,6);
  pending.slice(0,6).forEach(job=>job.done());await settle();
  assert(pending.slice(6,12).every(job=>job.url.includes('visible')));
  pending.slice(6,12).forEach(job=>job.done());await settle();
  assert.equal(pending.length,13);assert.equal(active,1);
  for(let i=12;i<16;i++){assert.equal(pending.length,i+1);pending[i].done();await settle();}
  await Promise.all(tasks);assert.equal(maxActive,6);assert.equal(ctx.__posterNetworkActive,0);
  console.log('PASS visible posters take the six shared slots; idle prefetch runs one at a time');

  let hits=0,release;
  const promote=env({fetch:()=>{hits++;return new Promise(resolve=>release=()=>resolve(image()));}});
  promote.__posterActive=1;
  const warm=promote.posterFetch('/api/tmdb/poster/promote.jpg',{prefetch:true,active:()=>false});
  await settle();assert.equal(hits,0);
  const visible=promote.posterFetch('/api/tmdb/poster/promote.jpg');
  await settle();assert.equal(hits,1);release();assert.strictEqual(await visible,await warm);
  await settle();assert.equal(promote.__posterWarmActive,0);
  console.log('PASS a queued prefetch entering the viewport is promoted without duplicate requests or deadlock');

  let network=0;
  const slow=env({window:{caches:{open:()=>new Promise(()=>{})}},fetch:async()=>{network++;return image();}});
  const started=Date.now();await slow.posterFetch('/api/tmdb/poster/slow-store.jpg');
  const elapsed=Date.now()-started;assert.equal(network,1);assert(elapsed>=80&&elapsed<600);
  console.log('PASS an indefinitely stalled browser cache cannot delay network fallback beyond the 100ms lookup budget');

  let stored,downloads=0;
  const cache={match:async()=>stored&&stored.clone(),put:async(url,response)=>{stored=response;},keys:async()=>[],delete:async()=>true};
  const cached=env({window:{caches:{open:async()=>cache}},fetch:async()=>{downloads++;return image();}});
  await cached.posterFetch('/api/emby/poster/123');await settle();assert.equal(downloads,1);
  cached.__posterMemory.clear();cached.__posterMemoryTimes.clear();cached.__posterMemoryBytes=0;
  await cached.posterFetch('/api/emby/poster/123');assert.equal(downloads,1);
  assert.equal(cached.__posterMemoryTimes.get('/api/emby/poster/123'),Number(stored.headers.get('X-StrimKeep-Cached-At')));
  stored=new Response(new Blob(['old']),{headers:{'X-StrimKeep-Cached-At':String(Date.now()-3600001)}});
  cached.__posterMemoryTimes.set('/api/emby/poster/123',Date.now()-3600001);
  await cached.posterFetch('/api/emby/poster/123');assert.equal(downloads,2);
  console.log('PASS persistent and memory cache expiry refresh Emby covers; revisits do not extend entry lifetime');

  const sized=env();
  assert.equal(sized.posterSizedUrl('/api/tmdb/poster/a.jpg',{getBoundingClientRect:()=>({width:110})}),'/api/tmdb/poster/a.jpg?size=w342');
  assert.equal(sized.posterSizedUrl('/api/tmdb/poster/a.jpg',{getBoundingClientRect:()=>({width:200})}),'/api/tmdb/poster/a.jpg?size=w500');
  assert.equal(sized.posterSizedUrl('/api/emby/poster/id?tag=x',{getBoundingClientRect:()=>({width:110})}),'/api/emby/poster/id?tag=x&size=w342');
  assert.equal(sized.posterSizedUrl('/api/tmdb/poster/a.jpg?size=w500'),'/api/tmdb/poster/a.jpg?size=w500');
  assert.equal(sized.posterSizedUrl('https://other.test/a.jpg'),'https://other.test/a.jpg');
  console.log('PASS thumbnails respect real width and pixel density; explicit size and external URLs stay intact');

  let attempts=0;
  const busy=env({setTimeout:(fn,ms)=>setTimeout(fn,Math.min(ms,2)),fetch:async()=>++attempts===1?new Response('',{status:503}):image()});
  await busy.posterFetch('/api/tmdb/poster/busy.jpg');assert.equal(attempts,2);
  attempts=0;busy.fetch=async()=>{attempts++;return new Response('',{status:503});};
  await assert.rejects(busy.posterFetch('/api/tmdb/poster/still-busy.jpg'),/HTTP 503/);
  assert.equal(attempts,2);assert(!busy.__posterMemory.has('/api/tmdb/poster/still-busy.jpg'));
  await settle();assert.equal(busy.__posterNetworkActive,0);
  console.log('PASS busy proxy retries only once, never caches failures, and releases its network slot');
  console.log('PASS 6 poster-speed groups; controlled DOM/network/cache substitutes, no NAS timing claims');
})().catch(error=>{console.error(error);process.exitCode=1;});
