const assert=require('assert'), vm=require('vm');
const source=require('./frontend_source.cjs').asset('/static/js/mapping.js');
const poster={src:'blob:existing'};
const info={innerHTML:'本地'};
const card={className:'poster-card old',getAttribute:k=>k==='data-movie-id'?'m1':'',
  querySelector:k=>k==='.info'?info:k==='img'?poster:null};
const nodes={'embyList':{querySelectorAll:()=>[card]},
 'emby-title-count':{textContent:'1'},'emby-total':{textContent:'1'},
 'emby-title-movies':{textContent:'1'}, 'emby-movies':{textContent:'1'},
 'emby-aligned':{textContent:'0'},'emby-missing':{textContent:'0'},
 'emby-extra':{textContent:'0'},'emby-ongoing':{textContent:'0'},
 'emby-unmatched':{textContent:'0'}};
const document={hidden:false,readyState:'loading',createElement:()=>({
  set innerHTML(value){this.firstElementChild={className:'poster-card new',
    querySelector:key=>key==='.info'?{innerHTML:'双库'}:null};}
})};
const ctx={document,window:null,Promise,Array,String,Set,Date,Object,Math,
  setInterval(){},setTimeout(){},embyLoaded:true,embyLibraryLoading:false,
  embyFilterState:{type:'movies',filter:'all',scope:'all'},
  embyData:{series:[],movies:[{id:'m1',in_local:true,in_share:false}],statsBase:{},stats:{}},
  $:name=>nodes[name]||{textContent:'',style:{}},
  api:async()=>({status:'success',version:1,full:false,series:[],movies:[{
    id:'m1',ids:['m1','m2'],in_local:true,in_share:true}],stats:{total_movies:1}}),
  renderMovieCard:()=>'<div class="poster-card"></div>',
  md_sync:(rows,stats)=>stats,updateEmbyChips(){},
  ensureExploreLibrarySync(){throw Error('unexpected explore refresh');},
  loadEmbyLibrary(){throw Error('unexpected full mapping reload');},
  renderEmbyList(){throw Error('poster should not be replaced');}
};
ctx.window=ctx;ctx.__activeTab='mapping';ctx.addEventListener=()=>{};
vm.runInNewContext(source.slice(source.indexOf('var libraryLiveVersion =')),ctx);
(async()=>{
  await ctx.pollLibraryLiveChanges();
  assert.equal(ctx.libraryLiveVersion,1);
  assert.equal(ctx.embyData.movies[0].in_share,true);
  assert.equal(info.innerHTML,'双库');
  assert.equal(card.querySelector('img'),poster);
  assert.equal(poster.src,'blob:existing');
  assert.equal(nodes['emby-movies'].textContent,1);
  console.log('PASS P1 movie cross-library live patch keeps existing poster node');
})().catch(e=>{console.error(e);process.exitCode=1;});
