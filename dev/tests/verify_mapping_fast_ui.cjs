// Actual mapping loader: one encoded request plus legacy migration compatibility.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/mapping.js'),'utf8');
function fixture(response,legacy=null){
 const nodes={},calls=[],renders=[],marks=[];
 const node=id=>nodes[id]||(nodes[id]={innerHTML:'',textContent:'',style:{}});
 const ctx=vm.createContext({window:{},localStorage:{getItem:()=>legacy},$:node,
  embyData:{series:[],movies:[],stats:{}},embyLibraryGeneration:0,embyLibraryLoading:false,embyLoaded:false,
  embyPage:1,embyShown:0,mappingPrefetch:null,
  api:async url=>{calls.push(url);return response;},
  mdLoad:async()=>{marks.push(1);ctx.window.__mdSet={migrated:{}};},
  md_sync:(rows,stats)=>stats,updateEmbyChips(){},renderEmbyList:()=>renders.push(ctx.embyData.series),
  toast(){},fmtAgo:String,esc:String});
 vm.runInContext(source.slice(source.indexOf('async function loadEmbyLibrary('),source.indexOf('var embyMoreLoading = false;')),ctx);
 return {ctx,calls,renders,marks,nodes};
}
(async()=>{
 const result={status:'success',from_cache:true,manual_done:{s1:{}},series:[{id:'s1',name:'fixture',_md:true}],movies:[],stats:{total_series:1}};
 let f=fixture(result);await f.ctx.loadEmbyLibrary(false,true,true);
 assert.equal(f.calls.length,1);assert(f.calls[0].endsWith('&web=1'));
 assert.equal(f.marks.length,0);assert(f.ctx.window.__mdSet.s1);assert.equal(f.renders.length,1);
 assert.equal(f.ctx.embyLibraryLoading,false);
 console.log('PASS Web cache reads include manual marks and render with one request');
 f=fixture(result,'{"migrated":{}}');await f.ctx.loadEmbyLibrary(false,true,true);
 assert.equal(f.marks.length,1);assert(f.ctx.window.__mdSet.migrated);assert.equal(f.renders.length,1);
 console.log('PASS old browser manual marks still migrate before rendering');
 f=fixture({...result,manual_done:undefined});await f.ctx.loadEmbyLibrary(false,true,true);
 assert.equal(f.marks.length,1);assert.equal(f.renders.length,1);
 console.log('PASS legacy/basic responses retain their manual-mark compatibility path');
 console.log('PASS 3/3; shipped loader with API/DOM substitutes');
})().catch(e=>{console.error(e);process.exitCode=1;});
