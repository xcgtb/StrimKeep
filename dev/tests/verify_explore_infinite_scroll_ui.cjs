// Real pagination/cancellation code with queued timers and a DOM substitute.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/explore.js'),'utf8');
const card=id=>({type:'movie',tmdb_id:String(id),title:'Movie '+id,poster:''});
const result=(ids,extra={})=>Object.assign({status:'success',cards:ids.map(card),total_pages:3},extra);
function environment(respond,columns=3){
  const timers=[],frames=[],requests=[],renders=[],toasts=[];
  const button={hidden:true},info={textContent:''},grid={innerHTML:'',querySelectorAll:()=>[]};
  const tab={classList:{contains:()=>true}},footer={style:{},getBoundingClientRect:()=>({top:100,bottom:130})};
  const boxes={exploreGrid:grid,explorePageInfo:info,exploreMoreBtn:button,'tab-explore':tab,explorePager:footer};
  let now=0;
  const ctx=vm.createContext({Date:{now:()=>now},console,AbortController,
    window:{IntersectionObserver:function(){},scrollY:720,innerHeight:800,scrollTo(){}},
    getComputedStyle:()=>({gridTemplateColumns:Array(columns).fill('100px').join(' ')}),
    $:id=>boxes[id],esc:String,toast:message=>toasts.push(message),
    api:(url,options)=>{requests.push({url,options});return Promise.resolve(respond(url,options));},
    setTimeout:(fn,ms)=>{timers.push(()=>{now+=ms;fn();});},
    requestAnimationFrame:fn=>{frames.push(fn);return frames.length;}});
  vm.runInContext(source,ctx);
  ctx.syncExploreCards=cards=>renders.push(cards.map(c=>c.tmdb_id));
  ctx.warmPosterUrls=()=>{};
  return {ctx,requests,renders,toasts,button,info,timers,frames,tab};
}
const tick=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
(async()=>{
  let e=environment(url=>new URL(url,'http://fixture').searchParams.get('page')==='1'
    ?result(Array.from({length:20},(_,i)=>i+1),{refreshing:true})
    :result(Array.from({length:20},(_,i)=>i+21)));
  await e.ctx.loadExplore();
  assert.equal(e.ctx.exploreLoading,false);assert.equal(Object.keys(e.ctx.exploreRefreshJobs).length,1);
  assert.equal(e.button.hidden,true);assert.equal(e.ctx.exploreShownCards.length,18);
  assert(e.requests.some(r=>r.url.includes('page=2')));
  await e.ctx.exploreMore();
  assert.equal(e.ctx.exploreState.page,2);assert.equal(e.ctx.exploreShownCards.length,36);
  assert.equal(new Set(e.ctx.exploreShownCards.map(c=>c.tmdb_id)).size,36);
  assert.equal(Object.keys(e.ctx.exploreRefreshJobs).length,1);
  console.log('PASS next-page prefetch and row appends proceed while library refresh is still waiting');

  let resolve;
  e=environment(url=>url.includes('page=1')?result(Array.from({length:20},(_,i)=>i+1)):
    new Promise(r=>{resolve=r;}));
  await e.ctx.loadExplore();const old=e.ctx.exploreMore();
  e.ctx.checkPosterAutoLoad();e.ctx.checkPosterAutoLoad();
  assert.equal(e.requests.filter(r=>r.url.includes('page=2')).length,1);
  const pending=e.requests.find(r=>r.url.includes('page=2'));
  e.ctx.pauseExplore();assert(pending.options.signal.aborted);
  assert.equal(e.ctx.exploreState.page,1);
  resolve(result([21]));await old;
  assert.equal(e.ctx.exploreShownCards.length,18);assert(!e.ctx.explorePages[2]);
  await e.ctx.ensureExploreLoaded();
  assert.equal(e.requests.filter(r=>r.url.includes('page=1')).length,1);
  console.log('PASS duplicate scroll events share one request; cancellation retains cursor/cards on return');

  let fail=true;
  e=environment(url=>url.includes('page=1')?result(Array.from({length:20},(_,i)=>i+1)):
    (fail?{status:'error',message:'offline'}:result([20,21,22],{total_pages:2})));
  await e.ctx.loadExplore();await e.ctx.exploreMore();
  assert(e.ctx.exploreMoreFailed);assert(!e.button.hidden);assert(e.info.textContent.includes('点击重试'));
  const n=e.requests.length;e.ctx.checkPosterAutoLoad();await tick();assert.equal(e.requests.length,n);
  fail=false;await e.ctx.exploreMore();
  assert(!e.ctx.exploreMoreFailed);assert(e.button.hidden);
  assert.equal(e.ctx.exploreShownCards.length,22);assert(e.info.textContent.includes('已全部加载'));
  console.log('PASS failed append stops auto retries, retains existing cards, and explicit retry deduplicates the final page');

  e=environment(()=>result([1,2],{total_pages:2}),25);
  await e.ctx.loadExplore();assert.equal(e.ctx.exploreShownCards.length,2);
  assert(e.ctx.posterNextCount(0,{},20)>=25);
  e.tab.classList.contains=()=>false;
  const inactiveRequests=e.requests.length;e.ctx.checkPosterAutoLoad();await tick();
  assert.equal(e.requests.length,inactiveRequests);
  console.log('PASS sparse/wide grids remain visible and inactive tabs do not trigger pagination');
  e=environment(()=>result([]));let resolveSubs,writes=[];
  e.ctx.currentSubs=[];e.ctx.exploreState.page=3;
  e.ctx.exploreSubscriptionsPromise=new Promise(r=>{resolveSubs=r;}).then(()=>{
    e.ctx.currentSubs=[{tmdb_id:'42',name:'Existing'}];return true;
  });
  e.ctx.api=async(url,options)=>{writes.push(JSON.parse(options.body));return {status:'success'};};
  const subscribe=e.ctx.toggleSubscribe('99','New','');
  await tick();assert.equal(writes.length,0);
  resolveSubs();await subscribe;
  assert.deepEqual(writes[0].subscriptions.map(s=>s.tmdb_id),['42','99']);
  assert.equal(e.ctx.exploreState.page,3);
  console.log('PASS subscribing waits for existing subscriptions and preserves the exploration cursor');
  console.log('PASS 5/5; queued timers and DOM substitutes, no live browser claims');
})().catch(e=>{console.error(e);process.exitCode=1;});
