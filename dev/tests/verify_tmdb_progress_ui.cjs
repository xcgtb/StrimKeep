// Exercise real launch/poll functions with delayed responses, without a live server.
const assert = require('assert'), vm = require('vm');
const {asset} = require('./frontend_source.cjs');
const source = asset('/static/js/mapping.js');
function fixture() {
  const calls = [], toasts = [], pending = [], intervals = new Map(), nodes = {};
  const node = id => nodes[id] || (nodes[id] = {innerHTML:'existing posters', textContent:'', style:{}, classList:{add(){},remove(){}}});
  let nextTimer = 0;
  const c = vm.createContext({document:{getElementById:node}, $:node,
    embyLibraryGeneration:0, embyLibraryLoading:false, embyLoaded:true,
    setInterval:fn=>{intervals.set(++nextTimer,fn);return nextTimer;}, clearInterval:id=>intervals.delete(id),
    toast:message=>toasts.push(message),
    api:url=>{calls.push(url);return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));}});
  vm.runInContext(source.slice(source.indexOf('var tmdbProgressTimer = null;'), source.indexOf('var embyMoreLoading = false;')), c);
  return {c,calls,toasts,pending,intervals,nodes};
}
const flush = async()=>{await Promise.resolve();await Promise.resolve();};
(async()=>{
  const f = fixture(), launch = f.c.loadEmbyLibrary(true,true);
  assert.deepEqual(f.calls, ['/api/emby/library?force=1&with_tmdb=1'], 'submit the new scan before polling old progress');
  f.pending.shift().resolve({status:'started'}); await launch;
  assert.equal(f.calls[1], '/api/tmdb/progress');
  f.pending.shift().resolve({progress:{running:true,percent:0}}); await flush();
  assert.equal(f.intervals.size,1); assert.equal(f.toasts.length,0);
  console.log('PASS launch waits for acceptance before watching the new scan');

  const g = fixture(); g.c.startTmdbProgressWatch();
  const old = g.pending.shift(), retry = g.c.loadEmbyLibrary(true,true);
  old.resolve({progress:{running:false,finished_at:1,error:'未配置 EMBY_KEY'}}); await flush();
  assert.deepEqual(g.toasts,[], 'an old in-flight progress response must not fail a retry');
  g.pending.shift().resolve({status:'started'}); await retry;
  g.pending.shift().resolve({progress:{running:false,finished_at:2,error:'Emby 连接失败'}}); await flush();
  assert.equal(g.toasts.length,1); assert.match(g.toasts[0],/Emby 连接失败/); assert.equal(g.intervals.size,0);
  console.log('PASS stale responses are ignored while actual new failures remain visible');

  const h = fixture(), initial = h.c.pollTmdbProgress();
  h.pending.shift().resolve({progress:{running:false,finished_at:1,error:'未配置 EMBY_KEY'}}); await initial;
  assert.deepEqual(h.toasts,[], 'opening the app must not replay a historical failure');
  const failedLaunch = h.c.loadEmbyLibrary(true,true);
  h.pending.shift().reject(Error('offline')); await failedLaunch;
  assert.equal(h.toasts.length,1); assert.match(h.toasts[0],/offline/);
  assert.equal(h.calls.length,2); assert.equal(h.c.embyLibraryLoading,false);
  assert.equal(h.nodes.embyList,undefined); // Existing wall was never replaced.
  console.log('PASS historical failures stay quiet and launch failures do not submit a second request');

  const k = fixture(), fastLaunch = k.c.loadEmbyLibrary(true,true);
  k.pending.shift().resolve({status:'started'}); await fastLaunch;
  const reloads=[]; k.c.loadEmbyLibrary=(...args)=>reloads.push(args);
  k.pending.shift().resolve({progress:{running:false,finished_at:2,error:''}}); await flush();
  assert.deepEqual(reloads,[[false,true]]); assert.equal(k.intervals.size,0);
  console.log('PASS scans finishing before the first poll still refresh the cached library');
})().catch(e=>{console.error(e);process.exitCode=1;});
