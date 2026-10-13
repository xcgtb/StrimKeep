// Actual shipped pagination VM: bypass cached URL, retry nocache and preserve existing posters.
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/mapping.js'),'utf8');
const start=source.indexOf('var mappingPagedActive = false;');
const end=source.indexOf('function mappingPreviewAllowed(',start);
assert(start>=0&&end>start);
const els=new Proxy({}, {get(t,k){return t[k]||(t[k]={value:'',textContent:'',style:{},dataset:{},innerHTML:'keep poster',classList:{contains:()=>false}})}});
const pending=[],ticks=[],toasts=[];
const c=vm.createContext({Object,Array,Math,Map,Date,Promise,AbortController,encodeURIComponent,
  window:{},$:(id)=>els[id],setTimeout:fn=>{ticks.push(fn);return 1;},clearTimeout:()=>{},
  embyFilterState:{type:'series',filter:'missing',scope:'share',cat:'',catMode:'seg'},
  embyData:{series:[],movies:[],stats:{}},embyPage:1,embyShown:0,embyLoaded:false,
  mappingPrefetch:null,embyView:{arr:[],fn:null},renderSeriesCard:x=>'<article>'+x.id+'</article>',
  renderMovieCard:x=>'<article>'+x.id+'</article>',
  mappingBatchSyncContext:()=>{}, replaceMappingCardsStable:(el,rows,render)=>{el.innerHTML=rows.map(render).join('');},
  hydratePosters:()=>{},schedulePosterAutoLoad:()=>{},updateEmbyChips:()=>{},
  updateEmbyLoadMore:()=>{},toast:(m)=>toasts.push(m),esc:String,
  api:(url,opts)=>new Promise(resolve=>pending.push({url,opts,resolve})),
  loadEmbyLibrary:()=>{throw Error('unexpected legacy fallback')}
});
vm.runInContext(source.slice(start,end),c);
const data={status:'success',rows:[{id:'ok'}],total:1,next_offset:1,facts_version:'v2',stats:{total_series:1},manual_done:{}};
(async()=>{
  // Existing page already loaded; a stale intermediate returns one nocache response.
  c.mappingPagedActive=true;c.embyLoaded=true;els.embyList.innerHTML='old poster';
  let p=c.loadMappingPaged(true);
  assert.equal(pending.length,1);
  assert.equal(pending[0].opts.cache,'no-store');
  assert(pending[0].url.includes('__skcb='));
  pending[0].resolve({status:'nocache'});
  await new Promise(setImmediate);
  assert.equal(pending.length,2);
  assert.notEqual(pending[0].url,pending[1].url);
  assert.equal(els.embyList.innerHTML,'old poster','no-cache cannot erase posters');
  pending[1].resolve(data);await p;
  assert.equal(els.embyList.innerHTML,'<article>ok</article>');
  assert.equal(c.mappingPagedRecoveryRetries,0);
  assert.equal(toasts.length,0);
  // Repeated nocache: preserve posters, stop automatic retries after one scheduled attempt.
  let again=c.loadMappingPaged(true);
  pending[2].resolve({status:'nocache'});
  await new Promise(setImmediate);
  pending[3].resolve({status:'nocache'});await again;
  assert.equal(els.embyList.innerHTML,'<article>ok</article>');
  assert.equal(ticks.length,1);
  assert(c.mappingPagedDirty);
  // The scheduled attempt can recover without changing selected filters.
  ticks.shift()();
  assert.equal(pending.length,5);
  pending[4].resolve(data);
  await new Promise(setImmediate);
  assert.equal(c.embyFilterState.scope,'share');
  console.log('PASS unique mapping URLs, no-store fetch, nocache retry, poster/filter preservation and bounded recovery');
})().catch(e=>{console.error(e);process.exitCode=1});
