// Exercise the actual shipped image pipeline: memory LRU and nonblocking disk writes.
const fs=require('fs'), path=require('path'), vm=require('vm'),assert=require('assert');
const src=fs.readFileSync(path.join(__dirname,'../../static/js/runtime.js'),'utf8');
const code=src.slice(src.indexOf('var POSTER_MAX_CONCURRENCY = 6;'),src.indexOf('function clearPosterLoading(img){'));
(async()=>{
  let opens=0, network=0, puts=0, unblock;
  const waiting=new Promise(resolve=>unblock=resolve);
  const cache={match:async()=>undefined,put:()=>{puts++;return waiting;},keys:async()=>[]};
  const ctx=vm.createContext({Map,Promise,Object,Set,console,AbortController,clearTimeout,setTimeout,
    window:{caches:{open:async()=>{opens++;return cache;}}},
    fetch:async()=>{network++;return {ok:true,clone(){return this;},blob:async()=>({size:5,marker:'test'})};}});
  vm.runInContext(code,ctx);
  // Two simultaneous image requests must share one image fetch and CacheStorage open.
  const first=ctx.posterFetch('/api/tmdb/poster/a.jpg');
  const second=ctx.posterFetch('/api/tmdb/poster/a.jpg');
  assert.strictEqual(await first,await second);
  assert.equal(network,1);assert.equal(opens,1);
  await Promise.resolve();assert.equal(puts,1);
  // The storage write has *not* completed; repeated view must already work.
  assert.equal((await ctx.posterFetch('/api/tmdb/poster/a.jpg')).marker,'test');
  assert.equal(network,1);
  // A bounded memory cache must evict old images, not retain a whole library.
  ctx.POSTER_MEMORY_MAX_BYTES=8;
  await ctx.posterFetch('/api/emby/poster/b');
  assert.equal(ctx.__posterMemory.size,1);
  assert.equal(ctx.__posterMemory.has('/api/tmdb/poster/a.jpg'),false);
  // Network outage must reject and be retriable, never cached as a poster.
  ctx.fetch=async()=>{throw Error('offline');};
  await assert.rejects(ctx.posterFetch('/api/emby/poster/offline'),/offline/);
  assert.equal(ctx.__posterInflight['/api/emby/poster/offline'],undefined);
  unblock();
  console.log('PASS 1/1; poster requests coalesce, render before slow CacheStorage writes, reuse bounded memory, and retry failures');
})().catch(err=>{console.error(err);process.exitCode=1;});
