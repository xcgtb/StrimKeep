// Isolated real P5 frontend: race, global pages, query encoding, lazy detail.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const shipped=fs.readFileSync(path.join(__dirname,'../../static/js/mapping.js'),'utf8');
const src=shipped.slice(shipped.indexOf('var mappingPagedActive = false;'), shipped.indexOf('function mappingPreviewAllowed('));
function node(){return {innerHTML:'', textContent:'',style:{},dataset:{},hidden:false,disabled:false,insertAdjacentHTML(side,txt){this.innerHTML+=txt;},getBoundingClientRect:()=>({top:0,bottom:500})};}
function environment(){
 const els=new Proxy({}, {get(target,key){if(!target[key])target[key]=node();return target[key];}});
 let requests=[], toasts=[], rows=new Map(), activeCalls=0;
 const context=vm.createContext({Object,Array,Math,Map,Date,Promise,AbortController,encodeURIComponent,
   window:{}, $:id=>els[id],setTimeout:(f)=>{return setTimeout(f,0);},clearTimeout,
   embyFilterState:{type:'series',scope:'all',filter:'all',cat:'',catMode:'seg'},
   embyData:{series:[],movies:[],stats:{}},embyPage:1,embyShown:0,embyLoaded:false,mappingPrefetch:null,
   embyView:{arr:[],fn:null},renderMovieCard:r=>'<div>'+r.id+'</div>',renderSeriesCard:r=>'<div>'+r.id+'</div>',
   replaceMappingCardsStable:(list,rows,render)=>{list.innerHTML=rows.map(render).join('');},
   toast:(msg)=>toasts.push(msg),esc:String,hydratePosters:()=>{},updateEmbyChips:()=>{},
   updateEmbyLoadMore(total){els.infoTotal.textContent=total;},schedulePosterAutoLoad(){},
   showEmbyDetailById(id){activeCalls++;},showMovieDetailById(id){activeCalls++;},
   api:(url,opts)=>new Promise((resolve,reject)=>{requests.push({url,opts,resolve,reject});}),
   loadEmbyLibrary:()=>{throw Error('unexpected legacy API')}
 });
 vm.runInContext(src,context);
 function response(request,items,total=items.length,version='rev-1'){
   const u=new URL(request.url,'http://test'); const offset=+(u.searchParams.get('offset')||0);
   request.resolve({status:'success',rows:items,total,next_offset:offset+items.length,has_more:offset+items.length<total,
     stats:{total_series:total,total_movies:0},facts_version:version,manual_done:{},emby_host:'http://emby.test'});
 }
 return {context,els,requests,response,toasts,getCalls:()=>activeCalls};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 let e=environment();
 let first=e.context.loadMappingPaged(true);assert.equal(e.requests.length,1);
 assert(e.requests[0].url.includes('limit=40') && e.requests[0].url.includes('media=series'));
 e.response(e.requests[0],[{id:'1'},{id:'2'}],3);await first;
 assert.equal(e.context.mappingPagedOffset,2);assert.equal(e.context.mappingPagedTotal,3);
 let next=e.context.loadMappingPaged(false);assert.equal(e.requests.length,2);
 e.response(e.requests[1],[{id:'3'}],3);await next;
 assert.equal(e.context.embyShown,3);assert.deepEqual(e.context.embyData.series.map(r=>r.id),['1','2','3']);
 assert.equal(e.context.mappingPagedTotal,3);
 console.log('PASS 1/5: first page and append use server offsets without whole-library fetch');
 e=environment();let outdated=e.context.loadMappingPaged(true);
 e.context.embyFilterState.scope='share';let newer=e.context.loadMappingPaged(true);
 assert(e.requests[0].opts.signal.aborted);e.response(e.requests[0],[{id:'old'}]);await outdated;
 e.response(e.requests[1],[{id:'new'}]);await newer;
 assert.deepEqual(e.context.embyData.series.map(x=>x.id),['new']);
 assert(e.requests[1].url.includes('scope=share'));
 console.log('PASS 2/5: outdated responses abort and cannot overwrite new filters');
 e=environment();let initial=e.context.loadMappingPaged(true);
 e.response(e.requests[0],[{id:'a'}],4,'v1');await initial;
 let more=e.context.loadMappingPaged(false);e.response(e.requests[1],[{id:'b'}],4,'v2');await tick();
 assert.equal(e.requests.length,3);e.response(e.requests[2],[{id:'c'}],1,'v2');await more;
 assert.deepEqual(e.context.embyData.series.map(x=>x.id),['c']);
 console.log('PASS 3/5: a revision change mid-pagination restarts at page one');
 e=environment();initial=e.context.loadMappingPaged(true);
 e.response(e.requests[0],[{id:'media-id'}]);await initial;
 assert(e.context.mappingNeedDetail('series','media-id'));
 assert.equal(e.requests.length,2);assert(e.requests[1].url.includes('/item?media=series&id=media-id'));
 e.requests[1].resolve({status:'success',facts_version:'rev-1',item:{id:'media-id',seasons:[{season:1}]}});
 await tick();await tick();
 assert.equal(e.getCalls(),1);
 assert.equal(e.context.mappingNeedDetail('series','media-id'),false);
 console.log('PASS 4/5: full detail fetched only after click, then cached by revision');
 e=environment();e.els.embySearch.value='电影 & 2026';
 e.context.embyFilterState={type:'movies',filter:'all',scope:'local',cat:'国产电影',catMode:'seg'};
 let url=e.context.mappingPagedQuery(0);let parsed=new URL(url,'http://test');
 assert.equal(parsed.searchParams.get('keyword'),'电影 & 2026');
 assert.equal(parsed.searchParams.get('category'),'国产电影');
 assert.equal(parsed.searchParams.get('media'),'movies');
 assert.equal(parsed.searchParams.get('scope'),'local');
 console.log('PASS 5/5: encoded category, movie mode, search and scope are sent to server');
 e=environment();initial=e.context.loadMappingPaged(true);
 e.response(e.requests[0],[{id:'old'}]);await initial;
 e.context.mappingNeedDetail('series','old');
 let changed=e.context.loadMappingPaged(true);
 e.response(e.requests[2],[{id:'new'}]);await changed;
 e.requests[1].resolve({status:'success',facts_version:'rev-1',item:{id:'old'}});
 await tick();await tick();
 assert.equal(e.getCalls(),0);
 assert.equal(e.context.window.__embySeriesMap.old,undefined);
 console.log('PASS 6/6: late detail from a previous filter cannot reopen or overwrite the current selection');
})().catch(err=>{console.error(err);process.exitCode=1;});
