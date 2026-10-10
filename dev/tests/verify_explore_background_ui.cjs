// Actual frontend functions with deterministic API/timer substitutes. No browser visual claim.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const source=html.slice(html.indexOf('function posterColumns(grid){'),html.indexOf('\nfunction syncExploreCards(cards, previous){'));
function environment(api){
  let now=0; const boxes={exploreGrid:{innerHTML:''},explorePageInfo:{textContent:''}};
  const renders=[],toasts=[],requests=[];
  const ctx=vm.createContext({Date:{now:()=>now},Object,Number,Math,Promise,encodeURIComponent,esc:String,
    exploreState:{region:'all',year:'',sort:'popularity',media:'tv',genre:'',page:1,totalPages:1,q:''},
    exploreGeneration:0,explorePages:{},exploreShownCards:[],exploreVisibleLimit:40,explorePrefetch:null,exploreLoading:false,
    getComputedStyle:()=>({gridTemplateColumns:'100px'}),schedulePosterAutoLoad(){},updateExploreLoadMore(){},$:id=>boxes[id],
    api:async(url,opts)=>{requests.push([url,opts]); return api(url,opts);},
    setTimeout(fn,ms){now+=ms; fn();},renderExploreCards:cards=>renders.push(JSON.parse(JSON.stringify(cards))),
    toast:m=>toasts.push(m),syncExploreCards:cards=>renders.push(JSON.parse(JSON.stringify(cards)))});
  vm.runInContext(source,ctx);ctx.ensureExplorePrefetch=()=>{};return {ctx,boxes,renders,toasts,requests};
}
const card=id=>({tmdb_id:String(id)});
const success=(cards,more={})=>Object.assign({status:'success',cards,total_pages:3,total_results:80},more);
(async()=>{
  let n=0;let e=environment(()=>++n===1?{status:'pending'}:success([card(1)]));
  await e.ctx.loadExplore();assert.equal(e.requests.length,2);assert.equal(e.renders[0][0].tmdb_id,'1');
  assert.equal(e.ctx.exploreLoading,false);assert(e.requests.every(x=>x[1].timeoutMs<=8000));
  console.log('PASS cold pages poll asynchronously and stop loading after success');
  n=0;e=environment(()=>++n===1?success([card(1)],{refreshing:true}):success([card(2)]));
  await e.ctx.loadExplore();assert.equal(e.ctx.exploreLoading,false);
  await Promise.all(Object.values(e.ctx.exploreRefreshJobs).map(j=>j.promise));
  assert.equal(e.renders.length,2);assert.equal(e.renders[0][0].tmdb_id,'1');
  assert.equal(e.renders[1][0].tmdb_id,'2');
  e.ctx.exploreState.page=2;n=0;
  e.ctx.exploreVisibleLimit=80;await e.ctx.loadExplore(true);
  await Promise.all(Object.values(e.ctx.exploreRefreshJobs).map(j=>j.promise));
  assert.deepEqual(e.renders.at(-1).map(c=>c.tmdb_id),['2']);
  assert.equal(Object.keys(e.ctx.explorePages).length,2);
  console.log('PASS stale cache renders first, refreshed pages replace their own page without duplicate appends');
  e=environment(()=>({status:'pending'}));await e.ctx.loadExplore();
  assert(e.requests.length<=25 && e.boxes.exploreGrid.innerHTML.includes('重新加载'));
  assert.equal(e.ctx.exploreLoading,false);
  assert.equal(e.boxes.explorePageInfo.textContent,'');
  console.log('PASS slow-service waiting is bounded and offers an explicit retry');
  let oldResolve,newResolve;e=environment(url=>new Promise(resolve=>{
    if(url.includes('q=new'))newResolve=resolve;else oldResolve=resolve;
  }));
  const old=e.ctx.loadExplore();e.ctx.exploreState.q='new';const fresh=e.ctx.loadExplore();
  oldResolve(success([card('old')]));await old;
  assert.equal(e.renders.length,0);assert.equal(e.ctx.exploreLoading,true);
  newResolve(success([card('new')]));await fresh;
  assert.equal(e.renders.at(-1)[0].tmdb_id,'new');assert.equal(e.ctx.exploreLoading,false);
  console.log('PASS filter changes supersede old results and old finalizers cannot clear the new loading state');
  e=environment(()=>{throw Error('fake timeout');});e.ctx.explorePages={1:[card(1)]};e.ctx.exploreState.page=2;
  await e.ctx.loadExplore(true);assert.equal(e.ctx.exploreState.page,1);assert.equal(e.toasts.length,1);
  assert.equal(e.ctx.explorePages[1][0].tmdb_id,'1');
  e=environment(()=>success([card(1)],{refresh_error:'更新失败'}));await e.ctx.loadExplore();
  assert.equal(e.requests.length,1);assert(e.boxes.explorePageInfo.textContent.includes('更新失败，显示缓存'));
  console.log('PASS append failure retains previous pages and refresh failure keeps visible cached cards');
  const statusCtx=vm.createContext({esc:String,libraryFactsLabel:()=>''});
  vm.runInContext(html.slice(html.indexOf('function exploreStatus(c){'),html.indexOf('\nfunction renderExploreCards(cards){')),statusCtx);
  assert.equal(statusCtx.exploreStatus({type:'movie',library_status:'unavailable'}).status,'片库待同步');
  assert.equal(statusCtx.exploreStatus({type:'tv',library_status:'stale'}).status,'片库待同步');
  assert.equal(statusCtx.exploreStatus({type:'movie',library_status:'available'}).status,'未入库');
  assert.equal(statusCtx.exploreStatus({type:'tv',library_status:'unavailable',eps:{have:0,total:20}}).status,'未入库');
  console.log('PASS unavailable identity data differs from a confirmed absence or zero episode count');
  n=0;e=environment(()=>++n===1?{status:'pending',message:'正在搜索 TMDB…'}:success([card(1)]));
  e.ctx.exploreState.q='新剧';await e.ctx.loadExplore(false,true);
  assert(e.requests[0][0].includes('&retry=1'));
  assert(!e.requests[1][0].includes('&retry=1'));
  e=environment(()=>({status:'error',message:'连接 TMDB 超时'}));
  await e.ctx.loadExplore();assert(!e.ctx.exploreLoading);
  assert(e.boxes.exploreGrid.innerHTML.includes('loadExplore(false,true)'));
  assert.equal(e.boxes.explorePageInfo.textContent,'');
  e=environment(()=>({status:'pending'}));e.ctx.exploreState.q='慢查询';
  await e.ctx.loadExplore();assert(e.requests.length<=15);
  assert.equal(e.boxes.explorePageInfo.textContent,'');
  console.log('PASS search waits are bounded, errors clear loading text, and manual retry resets only the first poll');
  console.log('PASS 7/7; DOM-substitute behavior checks only');
})().catch(e=>{console.error(e);process.exitCode=1;});
