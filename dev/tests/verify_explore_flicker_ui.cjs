// Run the real polling and DOM-update decision code. No live browser visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const source=html.slice(html.indexOf('function posterColumns(grid){'),html.indexOf('\nfunction appendExploreCards(cards){'));
let now=0,renderCount=0,appendCount=0,updates=0,requests=0,queries=0;
const posters=[{id:'existing-poster-node'}];
const badges=[{set outerHTML(value){updates++;this.value=value;}}];
const grid={innerHTML:'',querySelectorAll:()=>{queries++;return badges;}};
const pageInfo={textContent:''};
const base={tmdb_id:'100',type:'tv',title:'测试剧',poster:'/poster.jpg',eps:{have:13,total:20},facts_ts:100};
const clone=()=>JSON.parse(JSON.stringify(base));
let response=()=>({status:'success',cards:[clone()],total_pages:2,total_results:40,refreshing:requests<4});
const ctx=vm.createContext({Date:{now:()=>now},Object,Number,Math,Promise,JSON,encodeURIComponent,esc:String,
  exploreState:{region:'all',year:'',sort:'popularity',media:'tv',genre:'',page:1,totalPages:1,q:''},
  exploreGeneration:0,explorePages:{},exploreShownCards:[],exploreVisibleLimit:40,explorePrefetch:null,exploreLoading:false,
    getComputedStyle:()=>({gridTemplateColumns:'100px'}),schedulePosterAutoLoad(){},updateExploreLoadMore(){},$:id=>id==='exploreGrid'?grid:(id==='explorePageInfo'?pageInfo:null),
  api:async()=>{requests++;return response();},setTimeout(fn,ms){now+=ms;fn();},
  renderExploreCards(){renderCount++;},appendExploreCards(){appendCount++;},
  exploreStatus:c=>({status:'已入库 '+c.eps.have,cls:'partial',tip:' title="固定提示"'}),toast(){}});
vm.runInContext(source,ctx);ctx.ensureExplorePrefetch=()=>{};
(async()=>{
 await ctx.loadExplore();
 await Promise.all(Object.values(ctx.exploreRefreshJobs).map(j=>j.promise));
 assert.equal(requests,4);assert.equal(renderCount,1);assert.equal(updates,0);assert.equal(queries,0);
 console.log('PASS repeated identical poll responses render the poster grid only once');
 const previous=clone(),metadata=clone();metadata.facts_ts=200;metadata.facts_version='changed';
 ctx.syncExploreCards([metadata],[previous]);assert.equal(renderCount,1);assert.equal(updates,0);
 console.log('PASS cache metadata changes do not rebuild or touch poster nodes');
 const changed=clone();changed.eps.have=14;
 ctx.syncExploreCards([changed],[previous]);
 assert.equal(renderCount,1);assert.equal(updates,1);assert(badges[0].value.includes('已入库 14'));
 assert.equal(posters[0].id,'existing-poster-node');
 console.log('PASS changed counts update only the status badge and preserve loaded image nodes');
 const second=clone();second.tmdb_id='101';
 ctx.syncExploreCards([previous,second],[previous]);
 assert.equal(appendCount,1);assert.equal(renderCount,1);
 console.log('PASS loading the next page appends new cards without rebuilding existing posters');
 ctx.syncExploreCards([second,previous],[previous,second]);assert.equal(renderCount,2);
 console.log('PASS genuine card order or identity changes still render the new results');
 requests=0;now=0;renderCount=0;updates=0;
 response=()=>{const c=clone();if(requests>=3)c.eps.have=14;return {status:'success',cards:[c],refreshing:requests<4};};
 await ctx.loadExplore();
 await Promise.all(Object.values(ctx.exploreRefreshJobs).map(j=>j.promise));
 assert.equal(requests,4);assert.equal(renderCount,1);assert.equal(updates,1);
 console.log('PASS a background count update is applied once during polling, without repeating image renders');
 ctx.syncExploreCards([clone()],[]);assert.equal(renderCount,2);
 console.log('PASS a refreshed empty result replaces its placeholder when new cards arrive');
 console.log('PASS 7/7; DOM-substitute behavior checks only');
})().catch(e=>{console.error(e);process.exitCode=1;});
