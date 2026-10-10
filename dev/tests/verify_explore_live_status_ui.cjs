// Run shipped polling and badge patching with deterministic time and network.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/explore.js'),'utf8');
const movie=id=>({type:'movie',tmdb_id:String(id),title:'Movie '+id,poster:'/image.jpg',library_status:'unavailable'});
const tick=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
function environment(respond,initial=[movie(1)]){
  let now=0,next=0,active=true;const timers=new Map(),requests=[],events={},patches=[];
  const images=initial.map(()=>({id:'retained-image'}));
  const nodes=initial.map((_,i)=>({getBoundingClientRect:()=>({top:i*100,bottom:i*100+90})}));
  const badges=initial.map((_,i)=>({set outerHTML(value){patches.push({i,value});}}));
  const grid={querySelectorAll:selector=>selector.includes('badge-status')?badges:nodes};
  const document={hidden:false,addEventListener:(name,fn)=>events[name]=fn};
  const boxes={exploreGrid:grid,explorePageInfo:{textContent:''},'tab-explore':{classList:{contains:()=>active}}};
  const ctx=vm.createContext({console,Date,AbortController,document,window:{innerHeight:800,scrollY:650},
    $:id=>boxes[id],esc:String,getComputedStyle:()=>({gridTemplateColumns:'100px'}),
    api:(url,opts)=>{requests.push({url,opts,now});return Promise.resolve(respond(url,JSON.parse(opts.body),now));},
    setTimeout:(fn,ms)=>{timers.set(++next,{fn,time:now+ms});return next;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source,ctx);
  ctx.explorePages={1:initial};ctx.exploreShownCards=initial;ctx.exploreVisibleLimit=initial.length;
  ctx.exploreState.totalPages=1;
  ctx.renderExploreCards=()=>{throw Error('must not rebuild image nodes');};
  ctx.appendExploreCards=()=>{throw Error('must not append duplicate cards');};
  async function advance(){
    const [id,timer]=[...timers].sort((a,b)=>a[1].time-b[1].time)[0]||[];
    assert(timer,'expected future poll');timers.delete(id);now=timer.time;timer.fn();await tick();
  }
  return {ctx,requests,timers,events,document,patches,images,nodes,advance,boxes,setActive:v=>active=v};
}
const success=cards=>({status:'success',cards,library_refreshing:false});
(async()=>{
  let e=environment((url,body,now)=>success(body.cards.map(c=>({...c,library_status:now>=40000?'available':'unavailable',in_local:now>=40000}))));
  const image=e.images[0];e.ctx.ensureExploreLibrarySync();
  while(!e.ctx.exploreShownCards[0].in_local)await e.advance();
  assert(e.requests.at(-1).now>=40000);assert(e.patches.at(-1).value.includes('已完整'));
  assert(e.requests.every(r=>r.url==='/api/explore/library-status'));
  assert.equal(e.images[0],image);assert.equal(e.ctx.window.scrollY,650);assert.equal(e.ctx.exploreState.page,1);
  assert.equal(e.timers.size,1);e.ctx.stopExploreLibrarySync();
  console.log('PASS synchronization after 40 seconds patches badges without page reload, images, scroll or TMDB queries');

  let resolve;e=environment(()=>new Promise(r=>resolve=r));e.ctx.ensureExploreLibrarySync();await e.advance();
  const request=e.requests[0];e.ctx.pauseExplore();assert(request.opts.signal.aborted);assert(!e.timers.size);
  e.ctx.explorePages={1:[movie(2)]};e.ctx.exploreShownCards=e.ctx.explorePages[1];
  resolve(success([{...movie(1),in_local:true,library_status:'available'}]));await tick();
  assert.equal(e.ctx.exploreShownCards[0].tmdb_id,'2');assert(!e.ctx.exploreShownCards[0].in_local);assert(!e.timers.size);
  console.log('PASS cancelled or superseded replies cannot modify another filter or restart polling');

  e=environment((url,body)=>success(body.cards.map(c=>({...c,library_status:'available'}))));
  e.ctx.ensureExploreLibrarySync();e.document.hidden=true;e.events.visibilitychange();assert(!e.timers.size);
  e.document.hidden=false;e.events.visibilitychange();await e.advance();assert.equal(e.requests.length,1);
  e.setActive(false);await e.advance();assert.equal(e.requests.length,1);assert(!e.timers.size);
  e.setActive(true);e.ctx.ensureExploreLibrarySync(true);await e.advance();assert.equal(e.requests.length,2);
  e.ctx.stopExploreLibrarySync();
  console.log('PASS hidden/inactive pages stop; wake or return resumes badge updates');

  let n=0;e=environment((url,body)=>++n===1?Promise.reject(Error('offline')):success(body.cards.map(c=>({...c,in_share:true,library_status:'available'}))));
  e.ctx.ensureExploreLibrarySync();await e.advance();assert(e.boxes.explorePageInfo.textContent.includes('自动重试'));
  await e.advance();assert(e.ctx.exploreShownCards[0].in_share);assert(!e.ctx.exploreLibrarySync.notice);
  e.ctx.stopExploreLibrarySync();
  console.log('PASS network failure preserves cards and retries automatically');

  const tv={type:'tv',tmdb_id:'1',title:'Show',poster:'/show.jpg',eps:{have:3,total:20,match_status:'missing'},facts_ts:100};
  e=environment(()=>success([{...tv,eps:{have:20,total:20,match_status:'aligned'},facts_ts:200,library_status:'available'}]),[tv,movie(1)]);
  e.ctx.ensureExploreLibrarySync();await e.advance();assert.equal(e.ctx.exploreShownCards[0].eps.have,20);
  assert.equal(e.ctx.exploreShownCards[1].eps,undefined);assert(e.patches[0].value.includes('20/20'));e.ctx.stopExploreLibrarySync();
  e=environment((url,body)=>success(body.cards),Array.from({length:500},(_,i)=>movie(i+1)));
  e.nodes.forEach(n=>n.getBoundingClientRect=()=>({top:10,bottom:100}));
  assert.equal(e.ctx.exploreStatusTargets().length,200);
  e.nodes[0].getBoundingClientRect=()=>({top:-10000,bottom:-9000});
  assert.equal(e.ctx.exploreStatusTargets()[0].tmdb_id,'2');
  console.log('PASS TV episode facts and movie identities stay distinct; visible batches are bounded to 200');
  console.log('PASS 5/5; shipped handlers, simulated clock and DOM');
})().catch(e=>{console.error(e);process.exitCode=1;});
