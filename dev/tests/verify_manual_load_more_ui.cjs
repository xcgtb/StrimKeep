// Real frontend control flow with DOM/API substitutes; no live phone/browser visual claim.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
function box(){return {hidden:false,disabled:false,textContent:'',innerHTML:'',style:{},querySelectorAll:()=>[]};}
const items=Array.from({length:97},(_,id)=>({id}));
let frames=[],insertions=[],timers=0,hydrations=0,toasts=[];
const nodes={embyList:box(),embyInfText:box(),embyLoadMoreWrap:box(),embyMoreBtn:box()};
nodes.embyList.insertAdjacentHTML=(position,html)=>insertions.push(html);
const mapping=vm.createContext({Math,Promise,embyPage:1,embyShown:40,EMBY_PAGE:40,embyView:{arr:items,fn:x=>x.id+','},
 $:id=>nodes[id],posterNextCount:n=>n+40,prefetchMappingPosters(){},schedulePosterAutoLoad(){},requestAnimationFrame:fn=>frames.push(fn),setTimeout(fn){timers++;fn();},
 hydratePosters(){hydrations++;},toast:m=>toasts.push(m)});
vm.runInContext(html.slice(html.indexOf('var embyMoreLoading = false;'),html.indexOf('function updateEmbyChips(){')),mapping);
async function completeFrame(){assert.equal(frames.length,1);frames.shift()();await Promise.resolve();}
(async()=>{
 assert(html.includes('id="exploreMoreBtn"')&&html.includes('id="embyMoreBtn"'));
 assert(!html.includes('setupInfiniteScroll(')&&!html.includes('embyCheckMore(')&&!html.includes('继续下滑自动加载'));
 assert(html.includes('.load-more-footer .btn[hidden]{display:none}'));
 mapping.updateEmbyLoadMore(97);assert.equal(timers,0);assert.equal(mapping.embyPage,1);
 assert(nodes.embyInfText.textContent.includes('40 / 97')&&!nodes.embyMoreBtn.hidden);
 console.log('PASS both pages retain manual retry controls and footer updates do not duplicate batches');
 let task=mapping.embyMore();assert(mapping.embyMoreLoading&&nodes.embyMoreBtn.disabled);
 await mapping.embyMore();assert.equal(insertions.length,0);await completeFrame();await task;
 assert.equal(mapping.embyPage,2);assert.equal(insertions.length,1);
 assert.equal(insertions[0],items.slice(40,80).map(x=>x.id+',').join(''));
 assert(!nodes.embyMoreBtn.disabled&&hydrations===1);
 console.log('PASS mapping click appends one batch of 40 and duplicate clicks cannot skip a batch');
 task=mapping.embyMore();await completeFrame();await task;
 assert.equal(insertions[1],items.slice(80).map(x=>x.id+',').join(''));
 assert(nodes.embyMoreBtn.hidden&&nodes.embyInfText.textContent.includes('全部加载完毕'));
 await mapping.embyMore();assert.equal(insertions.length,2);
 console.log('PASS the final short batch hides its button and makes no extra append');
 mapping.embyPage=1;mapping.embyShown=40;mapping.embyView={arr:items,fn:x=>x.id+','};
 task=mapping.embyMore();mapping.embyView={arr:[{id:'new-filter'}],fn:x=>x.id};mapping.embyPage=1;mapping.embyShown=40;
 await completeFrame();await task;assert.equal(insertions.length,2);assert(!mapping.embyMoreLoading);
 console.log('PASS switching mapping filters during a pending append discards the old selection');
 mapping.embyView={arr:items,fn:()=>{throw Error('fake render failure');}};mapping.embyPage=1;mapping.embyShown=40;
 task=mapping.embyMore();await completeFrame();await task;
 assert.equal(mapping.embyPage,1);assert(!nodes.embyMoreBtn.disabled);assert.equal(toasts.length,1);
 mapping.embyView.fn=x=>x.id+',';task=mapping.embyMore();await completeFrame();await task;
 assert.equal(mapping.embyPage,2);assert.equal(insertions.at(-1),items.slice(40,80).map(x=>x.id+',').join(''));
 console.log('PASS mapping append failure keeps the same next batch available for retry');
 let apiResolve,apiCalls=0,rendered=[];
 const enodes={exploreMoreBtn:box(),exploreGrid:box(),explorePageInfo:box()};
 const c={type:'tv',tmdb_id:'1',title:'one'};
 const explore=vm.createContext({Object,JSON,Number,Math,Promise,Date,encodeURIComponent,esc:String,
  exploreState:{page:1,totalPages:3,region:'all',media:'tv',year:'',sort:'popularity',genre:'',q:'query'},
  exploreGeneration:0,explorePages:{1:[c]},exploreShownCards:[c],exploreVisibleLimit:40,explorePrefetch:null,exploreLoading:false,
  getComputedStyle:()=>({gridTemplateColumns:"100px"}),schedulePosterAutoLoad(){},exploreMoreFailed:false,
  $:id=>enodes[id],setTimeout:fn=>fn(),toast:m=>toasts.push(m),
  api:async url=>{apiCalls++;assert(url.includes('page=2')&&url.includes('q=query'));return new Promise(r=>apiResolve=r);},
  renderExploreCards:cards=>rendered.push(cards),appendExploreCards:cards=>rendered.push(cards),
  exploreStatus:()=>({status:'fixed',cls:'ok'})});
 vm.runInContext(html.slice(html.indexOf('function posterColumns(grid){'),html.indexOf('\nfunction appendExploreCards(cards){')),explore);explore.ensureExplorePrefetch=()=>{};
 task=explore.exploreMore();assert(enodes.exploreMoreBtn.disabled&&explore.exploreLoading);
 explore.exploreMore();assert.equal(apiCalls,1);
 apiResolve({status:'success',cards:[{type:'tv',tmdb_id:'2',title:'two'}],total_pages:3});await task;
 assert.equal(explore.exploreState.page,2);assert.equal(rendered[0].length,1);assert(!enodes.exploreMoreBtn.disabled);
 assert(enodes.explorePageInfo.textContent.includes('已显示 2 部'));
 console.log('PASS exploration manual fallback including search appends with a loading guard');
 explore.exploreState.page=1;explore.explorePages={1:[c]};explore.exploreShownCards=[c];
 explore.api=async()=>({status:'error',message:'fake network failure'});await explore.exploreMore();
 assert.equal(explore.exploreState.page,1);assert(enodes.exploreMoreBtn.textContent.includes('重试'));
 explore.api=async()=>({status:'success',cards:[],total_pages:2});await explore.exploreMore();
 assert.equal(explore.exploreState.page,2);assert(enodes.exploreMoreBtn.hidden);
 console.log('PASS failed exploration loads retry the same page and the last page hides the button');
 for(const [name,end] of [['appendExploreCards','function _subscribedTmdbIds'],['renderExploreCards','async function toggleSubscribe'],['renderSeriesCard','function showMovieDetailById'],['renderMovieCard','/* ═══════════ 追更订阅']]){
  const chunk=html.slice(html.indexOf('function '+name+'('),html.indexOf(end,html.indexOf('function '+name+'(')));
  assert(chunk.includes('loading="lazy"')&&chunk.includes('海报加载中'));
 }
 const removed=[],replaced=[],fakeParent={querySelector:()=>({remove:()=>removed.push(1)}),replaceChild:(next,old)=>replaced.push([next,old])};
 const pic={parentNode:fakeParent};const poster=vm.createContext({document:{createElement:()=>({})}});
 vm.runInContext(html.slice(html.indexOf('function clearPosterLoading(img){'),html.indexOf('async function hydratePosters(root){')),poster);
 poster.clearPosterLoading(pic);assert.equal(removed.length,1);
 poster.posterImageError(pic);assert.equal(replaced.length,1);assert.equal(replaced[0][0].textContent,'暂无海报');
 console.log('PASS both poster grids use native lazy loading with visible-image priority and loading/failure placeholders preserve other card nodes');
 const firstNodes={embyList:box(),embySearch:{value:''}};
 const first=vm.createContext({embyPage:1,EMBY_PAGE:40,embyFilterState:{type:'movies',scope:'all',filter:'all',cat:''},
  embyData:{movies:items,series:[]},embyView:{},embyShown:0,posterNextCount:n=>n+40,schedulePosterAutoLoad(){},$:id=>firstNodes[id],syncCatChip(){},catApply:a=>a,
  renderMovieCard:x=>'<card>'+x.id+'</card>',renderSeriesCard:x=>'<card>'+x.id+'</card>',
  hydratePosters(){},updateEmbyLoadMore(){},esc:String,Math});
 vm.runInContext(html.slice(html.indexOf('function renderEmbyList(){'),html.indexOf('function toggleSeasonList(id){')),first);
 first.renderEmbyList();assert.equal((firstNodes.embyList.innerHTML.match(/<card>/g)||[]).length,40);
 first.embyFilterState.type='series';first.embyData.series=items.map(x=>({id:x.id,name:String(x.id)}));
 first.renderEmbyList();assert.equal((firstNodes.embyList.innerHTML.match(/<card>/g)||[]).length,40);
 console.log('PASS initial movie and series mapping renders are limited to 40 cards');
 console.log('PASS 9/9; DOM-substitute behavior checks only');
})().catch(e=>{console.error(e);process.exitCode=1;});
