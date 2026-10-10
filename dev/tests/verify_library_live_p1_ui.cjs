// Exercise the actual P1 live update handlers, preserving already-loaded poster nodes.
const assert=require('assert'),vm=require('vm');
const source=require('./frontend_source.cjs').asset('/static/js/mapping.js');
let currentInfo={innerHTML:'before'}, badge={className:'before',textContent:'before'};
const poster={src:'blob:existing',changes:0};
const card={className:'poster-card old',getAttribute:k=>k==='data-series-id'?'s1':'',
  querySelector:k=>k==='.info'?currentInfo:k==='.badge-status'?badge:k==='img'?poster:null};
const nodes={
  embyList:{querySelectorAll:()=>[card]},
  'emby-title-count':{textContent:'1'},'emby-total':{textContent:'1'},
  'emby-aligned':{textContent:'0'},'emby-missing':{textContent:'1'},
  'emby-extra':{textContent:'0'},'emby-ongoing':{textContent:'0'},
  'emby-unmatched':{textContent:'0'}
};
const document={hidden:false,readyState:'loading',createElement:()=>({
  set innerHTML(v){this.firstElementChild={className:'poster-card new',
    querySelector:k=>k==='.info'?{innerHTML:'after 5/28'}:
      k==='.badge-status'?{className:'badge-status ok',textContent:'⏳ 在更'}:null};}
})};
const ctx={document,window:null,Promise,Array,String,Set,Date,Object,Math,
  setInterval(){},setTimeout(){},
  embyLoaded:true,embyLibraryLoading:false,
  embyFilterState:{filter:'all',scope:'all'},
  embyData:{series:[{id:'s1',have_eps:4,tmdb_info:{match_status:'missing'}}],movies:[],statsBase:{},stats:{}},
  $:name=>nodes[name]||{textContent:'',style:{}},
  api:async()=>({status:'success',version:2,full:false,series:[{
    id:'s1',have_eps:5,tmdb_info:{match_status:'ongoing'},series_ids:['s1']
  }],stats:{total_series:1,aligned:0,missing:0,ongoing:1}}),
  renderSeriesCard:()=>'<div class="poster-card"></div>',
  md_sync:(rows,stats)=>stats,updateEmbyChips(){},
  ensureExploreLibrarySync(){throw Error('unexpected explore page refresh');},
  loadEmbyLibrary(){throw Error('unexpected full reload');}
};
ctx.window=ctx;ctx.__activeTab='mapping';ctx.addEventListener=()=>{};
vm.runInNewContext(source.slice(source.indexOf('var libraryLiveVersion =')),ctx);
ctx.libraryLiveVersion=1;
(async()=>{
  await ctx.pollLibraryLiveChanges();
  assert.equal(ctx.libraryLiveVersion,2);
  assert.equal(ctx.embyData.series[0].have_eps,5);
  assert.equal(currentInfo.innerHTML,'after 5/28');
  assert.equal(badge.textContent,'⏳ 在更');
  assert.equal(card.querySelector('img'),poster);
  assert.equal(poster.src,'blob:existing');
  // A first /changes fetch may encounter a revision committed just after the
  // UI loaded. Do not record that revision without applying its fact patch.
  ctx.libraryLiveVersion=null;
  ctx.embyData.series[0].have_eps=4;
  currentInfo.innerHTML='before';
  badge.textContent='before';
  await ctx.pollLibraryLiveChanges();
  assert.equal(ctx.libraryLiveVersion,2);
  assert.equal(ctx.embyData.series[0].have_eps,5);
  assert.equal(currentInfo.innerHTML,'after 5/28');
  assert.equal(card.querySelector('img'),poster);
  console.log('PASS P1 initial revision race and subsequent patches preserve the poster node');
})().catch(e=>{console.error(e);process.exitCode=1;});
