// Real frontend functions, deterministic DOM/network substitutes; not a browser visual test.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const exploreSource=html.slice(html.indexOf('function posterColumns(grid){'),html.indexOf('\nfunction appendExploreCards(cards){'));
const mappingSource=html.slice(html.indexOf('var embyMoreLoading = false;'),html.indexOf('function updateEmbyChips(){'));
const autoSource=html.slice(html.indexOf('var posterAutoFrame = null;'),html.indexOf('function _subscribedTmdbIds(){'));
const cards=(start,count)=>Array.from({length:count},(_,i)=>({tmdb_id:String(start+i),type:'movie',title:'Film '+(start+i),poster:'/p'+(start+i)+'.jpg'}));
function node(){return {hidden:false,disabled:false,style:{},textContent:'',innerHTML:'',querySelectorAll:()=>[]};}
function environment(columns=3){
 let cols=columns,now=0;const requests=[],warm=[],paint=[],toasts=[];
 const nodes={exploreGrid:node(),exploreMoreBtn:node(),explorePageInfo:node()};
 const pages={1:{status:'success',cards:cards(0,40),total_pages:2,total_results:80},2:{status:'success',cards:cards(40,40),total_pages:2,total_results:80}};
 const ctx=vm.createContext({Object,Number,Math,JSON,Promise,encodeURIComponent,esc:String,Date:{now:()=>now},
  exploreState:{page:1,totalPages:1,region:'all',year:'',sort:'popularity',media:'movie',genre:'',q:''},
  exploreGeneration:0,explorePages:{},exploreShownCards:[],exploreVisibleLimit:40,explorePrefetch:null,exploreLoading:false,exploreMoreFailed:false,
  $:id=>nodes[id],getComputedStyle:()=>({gridTemplateColumns:Array(cols).fill('100px').join(' ')}),
  schedulePosterAutoLoad(){},warmPosterUrls:(urls,active)=>warm.push({urls,active}),
  renderExploreCards:a=>paint.push({kind:'render',ids:a.map(x=>x.tmdb_id)}),appendExploreCards:a=>paint.push({kind:'append',ids:a.map(x=>x.tmdb_id)}),
  exploreStatus:()=>({status:'same',cls:'ok'}),toast:m=>toasts.push(m),setTimeout(fn,ms){now+=ms;fn();},
  api:async(url,opts)=>{requests.push({url,opts});return pages[Number(new URL(url,'http://fixture').searchParams.get('page'))];}});
 vm.runInContext(exploreSource,ctx);
 return {ctx,nodes,pages,requests,warm,paint,toasts,setColumns:n=>cols=n};
}
function ids(e){return Array.from(e.ctx.exploreShownCards,x=>x.tmdb_id);}
(async()=>{
 const styleBlocks=Array.from(html.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g),x=>x[1]);
 const rules=styleBlocks.join('\n').match(/[^{}]+\{[^{}]*\}/g)||[];
 assert(!rules.some(r=>r.split('{')[0].includes('#explorePager')&&/display\s*:\s*none\s*!important/.test(r)));
 assert(html.includes('setupPosterAutoLoad();')&&html.includes('id="exploreMoreBtn"')&&html.includes('id="embyMoreBtn"'));
 console.log('PASS the exploration footer has no late stylesheet hiding rule and both manual fallbacks remain');
 for(const [cols,shown] of [[3,39],[4,40],[7,35]]){
  const e=environment(cols);await e.ctx.loadExplore();assert.equal(ids(e).length,shown);assert.equal(ids(e).length%cols,0);
  assert.equal(e.ctx.exploreAllCards().length,40);assert(!e.nodes.exploreMoreBtn.hidden);
 }
 console.log('PASS real 3, 4 and 7 column layouts render full initial rows while retaining unused cards');
 let e=environment();await e.ctx.loadExplore();await e.ctx.explorePrefetch.promise;
 assert.equal(e.requests.length,2);assert(e.requests[1].url.includes('page=2'));assert.equal(e.warm.length,1);assert.equal(ids(e).length,39);
 const entry=e.ctx.explorePrefetch;e.ctx.ensureExplorePrefetch();assert.strictEqual(e.ctx.explorePrefetch,entry);assert.equal(e.requests.length,2);
 console.log('PASS one lookahead request warms next-page posters without displaying them or duplicating prefetch');
 await e.ctx.exploreMore();assert.equal(e.requests.length,2);assert.equal(ids(e).length,78);
 assert.deepEqual(ids(e),cards(0,78).map(x=>x.tmdb_id));assert.equal(e.paint.filter(x=>x.kind==='render').length,1);
 assert.equal(e.paint.at(-1).kind,'append');assert(!e.nodes.exploreMoreBtn.hidden);
 await e.ctx.exploreMore();assert.deepEqual(ids(e),cards(0,80).map(x=>x.tmdb_id));assert(e.nodes.exploreMoreBtn.hidden);
 await e.ctx.exploreMore();assert.equal(e.requests.length,2);
 console.log('PASS prefetched data appends without a cold request or redraw, and final remainder loses no cards or adds fake ones');
 e=environment();e.pages[1].total_pages=3;e.pages[2].total_pages=3;e.pages[3]={status:'success',cards:cards(80,13),total_pages:3,total_results:93};
 await e.ctx.loadExplore();await e.ctx.exploreMore();assert.equal(ids(e).length,78);
 await e.ctx.exploreMore();assert.deepEqual(ids(e),cards(0,93).map(x=>x.tmdb_id));assert.equal(e.requests.length,3);
 console.log('PASS retained boundary cards carry into later pages and the real final short batch remains complete');
 e=environment();await e.ctx.loadExplore();e.setColumns(4);await e.ctx.exploreMore();assert.equal(ids(e).length,76);assert.equal(ids(e).length%4,0);
 await e.ctx.exploreMore();assert.equal(ids(e).length,80);
 console.log('PASS the next append adapts to resized columns without dropping previously shown cards');
 e=environment();let resolve;e.ctx.exploreState.q='old search';e.ctx.api=async url=>{e.requests.push({url});return new Promise(r=>resolve=r);};
 e.ctx.exploreState.totalPages=2;e.ctx.ensureExplorePrefetch();const old=e.ctx.explorePrefetch;
 e.ctx.exploreState.q='new search';e.ctx.exploreGeneration++;resolve(e.pages[2]);const stale=await old.promise;
 assert.equal(stale.status,'cancelled');assert.equal(e.warm.length,0);assert.equal(ids(e).length,0);assert(e.requests[0].url.includes('q=old%20search'));
 console.log('PASS prefetch captures its filters and superseded results cannot warm or replace the new view');
 e=environment();e.pages[2]={status:'error',message:'fixture failure'};await e.ctx.loadExplore();await e.ctx.exploreMore();
 assert.equal(e.ctx.exploreState.page,1);assert.equal(ids(e).length,39);assert(e.ctx.exploreMoreFailed);assert(e.nodes.exploreMoreBtn.textContent.includes('重试'));
 e.pages[2]={status:'success',cards:cards(40,40),total_pages:2};await e.ctx.exploreMore();assert.equal(e.ctx.exploreState.page,2);assert.equal(ids(e).length,78);
 console.log('PASS failed prefetch preserves the same page and visible list, then manual retry loads that page');
 e=environment();e.pages[1].cards=[];await e.ctx.loadExplore();assert.equal(ids(e).length,0);await e.ctx.exploreMore();assert.equal(ids(e).length,39);
 await e.ctx.exploreMore();assert.equal(ids(e).length,40);
 console.log('PASS sparse or empty early pages do not hide the next page or discard a queued tail');
 e=environment();e.ctx.exploreState.totalPages=2;e.ctx.api=async()=>{e.requests.push({});return {status:'pending'};};
 e.ctx.ensureExplorePrefetch();const waiting=await e.ctx.explorePrefetch.promise;assert.equal(waiting.status,'error');assert(e.requests.length<=25);assert.equal(ids(e).length,0);
 console.log('PASS pending lookahead has a bounded wait and never mutates the displayed list');
 const mNodes={embyList:node(),embyInfText:node(),embyMoreBtn:node(),embyLoadMoreWrap:node()};let cols=3,insertions=[],frames=[];
 mNodes.embyList.insertAdjacentHTML=(pos,s)=>insertions.push(s);
 const items=Array.from({length:97},(_,i)=>i);
 const m=vm.createContext({Math,Promise,embyView:{arr:items,fn:x=>x+','},embyPage:1,embyShown:39,
  $:id=>mNodes[id],getComputedStyle:()=>({gridTemplateColumns:Array(cols).fill('100px').join(' ')}),
  hydratePosters(){},prefetchMappingPosters(){},schedulePosterAutoLoad(){},toast(){},requestAnimationFrame:fn=>frames.push(fn),setTimeout:fn=>fn()});
 vm.runInContext(html.slice(html.indexOf('function posterColumns(grid){'),html.indexOf('function exploreAllCards(){'))+mappingSource,m);
 let task=m.embyMore();m.embyMore();assert.equal(frames.length,1);frames.shift()();await task;assert.equal(m.embyShown,78);
 cols=4;task=m.embyMore();frames.shift()();await task;assert.equal(m.embyShown,97);
 assert.equal(insertions.join(''),items.slice(39).map(x=>x+',').join(''));assert(mNodes.embyMoreBtn.hidden);
 console.log('PASS mapping uses its actual shown cursor across column changes, duplicate clicks and the short final batch');
 let warmed=[];m.mappingPrefetch=null;m.embyShown=0;m.embyView={arr:items.map(i=>({id:i,has_image:true})),fn:()=>''};
 m.warmPosterUrls=(urls,active)=>warmed.push({urls,active});
 vm.runInContext(html.slice(html.indexOf('function prefetchMappingPosters(){'),html.indexOf('var posterAutoFrame = null;')),m);
 m.prefetchMappingPosters();m.prefetchMappingPosters();assert.equal(warmed.length,1);assert.equal(warmed[0].urls.length,40);assert(warmed[0].active());
 m.embyView={arr:[],fn:null};assert(!warmed[0].active());
 console.log('PASS mapping image warming is limited to one next batch and stops scheduling old-filter posters');
 let observers=[],events=[],autoFrames=[],exploreLoads=0,mappingLoads=0;
 const aNodes={};for(const [tab,footer] of [['tab-explore','explorePager'],['tab-mapping','embyLoadMoreWrap']]){
  aNodes[tab]={active:tab==='tab-explore',classList:{contains:()=>aNodes[tab].active}};
  aNodes[footer]={style:{},getBoundingClientRect:()=>({top:1600,bottom:1680})};
 }
 const a=vm.createContext({$:id=>aNodes[id],window:{innerHeight:800,IntersectionObserver:true,addEventListener:(...args)=>events.push(args)},
  IntersectionObserver:class {constructor(fn,options){this.fn=fn;this.options=options;this.targets=[];observers.push(this);}observe(el){this.targets.push(el);}},
  requestAnimationFrame:fn=>{autoFrames.push(fn);return autoFrames.length;},exploreMoreFailed:false,embyMoreFailed:false,
  exploreMore:()=>exploreLoads++,embyMore:()=>mappingLoads++});
 vm.runInContext(autoSource,a);a.setupPosterAutoLoad();a.setupPosterAutoLoad();assert.equal(observers.length,1);assert.equal(observers[0].options.rootMargin,'900px 0px');assert.equal(observers[0].targets.length,2);
 assert.deepEqual(events.map(x=>x[0]),['scroll','resize']);assert(events.every(x=>x[2].passive));
 a.schedulePosterAutoLoad();a.schedulePosterAutoLoad();assert.equal(autoFrames.length,1);autoFrames.shift()();assert.equal(exploreLoads,1);assert.equal(mappingLoads,0);
 a.exploreMoreFailed=true;a.checkPosterAutoLoad();assert.equal(exploreLoads,1);
 aNodes['tab-explore'].active=false;aNodes['tab-mapping'].active=true;a.checkPosterAutoLoad();assert.equal(mappingLoads,1);
 aNodes.embyLoadMoreWrap.style.display='none';a.checkPosterAutoLoad();assert.equal(mappingLoads,1);
 console.log('PASS scrolling starts ahead of the viewport, coalesces events, excludes hidden tabs and pauses after failure');
 let timer,cleared=0,signal,aborted=false;
 const p=vm.createContext({window:{},__posterInflight:{},POSTER_CACHE_NAME:'fixture',
  AbortController:class {constructor(){this.signal={};}abort(){aborted=true;}},setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>cleared++,
  fetch:async(url,opts)=>{signal=opts.signal;return {ok:true,clone(){return this;},blob:async()=>({blob:true})};}});
 vm.runInContext(html.slice(html.indexOf('async function posterFetch(url){'),html.indexOf('function clearPosterLoading(img){')),p);
 assert((await p.posterFetch('/api/emby/poster/1')).blob);assert(signal);assert.equal(cleared,1);assert.equal(Object.keys(p.__posterInflight).length,0);
 p.fetch=(url,opts)=>new Promise((resolve,reject)=>{p.AbortController.prototype.abort=function(){aborted=true;reject(Error('timed out'));};});
 const slow=p.posterFetch('/api/emby/poster/2');await Promise.resolve();timer();await assert.rejects(slow,/timed out/);assert(aborted);assert.equal(cleared,2);
 console.log('PASS authenticated poster network loads release their timer and abort a stalled fetch');
 console.log('PASS 14/14; DOM-substitute behavior checks only');
})().catch(e=>{console.error(e);process.exitCode=1;});
