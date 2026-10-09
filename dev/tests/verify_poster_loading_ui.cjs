// Real application functions with controlled DOM/network substitutes.
const vm=require('vm'),assert=require('assert');
const html=require('./frontend_source.cjs').loadFrontend();
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function box(){return {innerHTML:'',textContent:'',style:{},querySelectorAll:()=>[],hidden:false};}
function exploreEnv(){
 const nodes={exploreGrid:box(),exploreMoreBtn:box(),explorePageInfo:box()},requests=[],paints=[],frames=[],scrolls=[];
 const pages={1:{status:'success',cards:Array.from({length:40},(_,id)=>({type:'movie',tmdb_id:id+1,title:'title'+id})),total_pages:2},
  2:{status:'success',cards:Array.from({length:40},(_,id)=>({type:'movie',tmdb_id:id+31,title:'title'+(id+30)})),total_pages:2}};
 const ctx=vm.createContext({AbortController,Date,Object,Number,Math,JSON,Promise,Set,encodeURIComponent,esc:String,
  exploreState:{page:1,totalPages:1,region:'all',media:'movie',q:''},exploreGeneration:0,explorePages:{},exploreShownCards:[],
  exploreLoading:false,exploreMoreFailed:false,exploreVisibleLimit:40,explorePrefetch:null,$:id=>nodes[id],
  window:{scrollY:350,scrollTo:(x,y)=>scrolls.push(y)},requestAnimationFrame:fn=>frames.push(fn),
  getComputedStyle:()=>({gridTemplateColumns:'100px 100px 100px'}),schedulePosterAutoLoad(){},warmPosterUrls(){},
  renderExploreCards:cards=>paints.push(cards),appendExploreCards:cards=>paints.push(cards),exploreStatus:()=>({status:'fixed'}),toast(){},
  setTimeout:fn=>fn(),api:async(url,opts)=>{requests.push({url,opts});return pages[Number(new URL(url,'http://fixture').searchParams.get('page'))];}});
 vm.runInContext(html.slice(html.indexOf('function posterColumns(grid){'),html.indexOf('\nfunction appendExploreCards(cards){')),ctx);
 return {ctx,nodes,requests,paints,frames,scrolls};
}
(async()=>{
 let e=exploreEnv();await e.ctx.loadExplore();await e.ctx.exploreMore();
 assert.equal(e.ctx.exploreShownCards.length,70);assert.equal(new Set(e.ctx.exploreShownCards.map(c=>c.tmdb_id)).size,70);
 const count=e.requests.length,paint=e.paints.length,shown=e.ctx.exploreShownCards;
 e.ctx.pauseExplore();e.ctx.ensureExploreLoaded();e.frames.shift()();
 assert.equal(e.requests.length,count);assert.equal(e.paints.length,paint);assert.strictEqual(e.ctx.exploreShownCards,shown);assert.deepEqual(e.scrolls,[350]);
 console.log('PASS overlapping pages deduplicate by media identity; returning reuses cards, cursor and scroll without new data requests');
 e=exploreEnv();let oldResolve,newResolve;
 e.ctx.api=(url,opts)=>{e.requests.push({url,opts});return new Promise(resolve=>url.includes('q=new')?newResolve=resolve:oldResolve=resolve);};
 const old=e.ctx.loadExplore();e.ctx.exploreState.q='new';const current=e.ctx.loadExplore();
 assert(e.requests[0].opts.signal.aborted);oldResolve({status:'success',cards:[{tmdb_id:'old'}]});await old;
 assert.equal(e.paints.length,0);newResolve({status:'success',cards:[{tmdb_id:'new'}]});await current;
 assert.equal(e.ctx.exploreShownCards[0].tmdb_id,'new');
 console.log('PASS changing filters aborts the obsolete foreground request and ignores its late response');
 let signal,timers=0;
 const apiCtx=vm.createContext({Object,AbortController,getHeaders:()=>({}),getHeadersCalls:0,
  setTimeout:()=>++timers,clearTimeout:()=>timers--,showLoginOverlay(){},
  fetch:(url,opts)=>{signal=opts.signal;return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>{const error=Error('abort');error.name='AbortError';reject(error);}));}});
 vm.runInContext(html.slice(html.indexOf('async function api(path, opts){'),html.indexOf('async function pollTask(')),apiCtx);
 const parent=new AbortController();const task=apiCtx.api('/fixture',{signal:parent.signal,timeoutMs:8000});parent.abort();
 await assert.rejects(task);assert(signal.aborted);assert.equal(timers,0);
 console.log('PASS API timeouts preserve caller cancellation and release timers');
 const observers=[],requests=[],urls=[],revoked=[];let active=0,maxActive=0;
 const pics=Array.from({length:16},(_,id)=>({id,isConnected:true,attrs:{'data-emby-src':'/poster/'+id},listeners:{},
  getAttribute(key){return this.attrs[key];},setAttribute(key,value){this.attrs[key]=value;},removeAttribute(key){delete this.attrs[key];},
  getBoundingClientRect(){return {width:120,top:id%8<4?0:3000,bottom:id%8<4?180:3180};},
  addEventListener(key,fn){(this.listeners[key]||(this.listeners[key]=[])).push(fn);},
  removeEventListener(key,fn){this.listeners[key]=(this.listeners[key]||[]).filter(x=>x!==fn);},
  parentNode:{querySelector:()=>({remove(){}}),replaceChild(){}}}));
 const p=vm.createContext({Promise,POSTER_MAX_CONCURRENCY:6,window:{innerHeight:800,IntersectionObserver:true},
  IntersectionObserver:class{constructor(fn){this.fn=fn;this.targets=[];observers.push(this);}observe(img){if(!this.targets.includes(img))this.targets.push(img);}unobserve(){}},
  URL:{createObjectURL:()=>{const url='blob:'+urls.length;urls.push(url);return url;},revokeObjectURL:url=>revoked.push(url)},
  clearPosterLoading(){},posterImageError(){},posterFetch:url=>{active++;maxActive=Math.max(maxActive,active);return new Promise(resolve=>requests.push({url,resolve:value=>{active--;resolve(value);}}));}});
 vm.runInContext(html.slice(html.indexOf('var __posterQueue ='),html.indexOf('function renderMenu(){')),p);
 function root(images){return {querySelectorAll:selector=>selector.includes('data-emby-src')?images.filter(img=>!img.attrs['data-loaded']):images};}
 const first=p.hydratePosters(root(pics.slice(0,8))),second=p.hydratePosters(root(pics.slice(8)));
 assert.equal(requests.length,6);assert.equal(observers[0].targets.length,8);assert(pics[0].loading==='eager'&&pics[0].fetchPriority==='high');assert(!pics[4].fetchPriority);
 requests.slice().forEach(r=>r.resolve({}));await tick();assert.equal(requests.length,8);requests.slice(6).forEach(r=>r.resolve({}));await Promise.all([first,second]);
 assert.equal(maxActive,6);assert(!requests.some(r=>Number(r.url.split('/').pop())%8>=4));
 pics[0].listeners.load.slice().forEach(fn=>fn());pics[1].listeners.error.slice().forEach(fn=>fn());assert.equal(new Set(revoked).size,2);
 observers[0].fn(observers[0].targets.map(target=>({target,isIntersecting:true})));assert.equal(requests.length,14);assert.equal(maxActive,6);
 requests.slice(8).forEach(r=>r.resolve({}));await tick();requests.slice(14).forEach(r=>r.resolve({}));await tick();
 assert.equal(requests.length,16);assert.equal(maxActive,6);
 console.log('PASS authenticated images load near the viewport, share six slots across roots, and release blob URLs on load or error');
 const nodes=new Proxy({}, {get:(target,key)=>target[key]||(target[key]=box())});nodes.embyList.innerHTML='existing posters';
 let resolveFirst,resolveLast;const toasts=[];
 const m=vm.createContext({embyLibraryGeneration:0,embyLibraryLoading:false,embyLoaded:true,embyPage:3,embyShown:120,mappingPrefetch:{},
  embyData:{series:[],movies:[],stats:{}},$:id=>nodes[id],window:{},api:()=>new Promise(resolve=>!resolveFirst?resolveFirst=resolve:resolveLast=resolve),
  mdLoad:async()=>{},md_sync:(series,stats)=>stats,updateEmbyChips(){},renderEmbyList(){nodes.embyList.innerHTML=m.embyData.series[0].name;},
  startTmdbProgressWatch(){},toast:message=>toasts.push(message),esc:String,fmtAgo:String});
 vm.runInContext(html.slice(html.indexOf('async function loadEmbyLibrary('),html.indexOf('var embyMoreLoading = false;')),m);
 const older=m.loadEmbyLibrary(false,true,false),newer=m.loadEmbyLibrary(false,true,false);assert.equal(nodes.embyList.innerHTML,'existing posters');
 resolveLast({status:'success',series:[{id:'new',name:'new posters'}]});await newer;resolveFirst({status:'success',series:[{id:'old',name:'old posters'}]});await older;
 assert.equal(nodes.embyList.innerHTML,'new posters');assert(!m.embyLibraryLoading);
 m.api=async()=>{throw Error('offline');};await m.loadEmbyLibrary(false,true,false);assert.equal(nodes.embyList.innerHTML,'new posters');assert.equal(toasts.length,1);
 console.log('PASS mapping refresh preserves the visible list on failure and rejects obsolete responses');
 console.log('PASS 5/5; DOM/network substitutes, no live browser or NAS claims');
})().catch(error=>{console.error(error);process.exitCode=1;});
