// Regression check against the actual shipped P4 JS; no browser performance claim.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(path.resolve(__dirname,'../../static/js/mapping.js'),'utf8');
const start=source.indexOf('var embyLoaded = false;');
const end=source.indexOf('var embyLibraryLoading',start);
assert(start>0 && end>start && source.includes('function replaceMappingCardsStable'));
const part=source.slice(start,end);
function fixture(){
  const list={innerHTML:'initial',dataset:{},querySelectorAll:()=>[]},hint={textContent:''};
  const map={embyList:list,embyInfText:hint,embySearch:{value:''}};
  let resolveRequest;
  const ctx=vm.createContext({AbortController,Object,Array,Promise,console,window:{},
    embyFilterState:{type:'series',filter:'all',scope:'all',cat:''},embyLibraryGeneration:4,
    $:id=>map[id],posterNextCount:()=>40,renderSeriesCard:x=>'<article>'+x.id+'</article>',
    hydratePosters:()=>{},api:()=>new Promise(r=>resolveRequest=r)});
  vm.runInContext(part,ctx);
  return {ctx,list,hint,respond:x=>resolveRequest(x)};
}
(async()=>{
  let t=fixture();
  const pending=t.ctx.showMappingFirstScreen(4);
  t.respond({status:'success',series:[{id:'1'},{id:'2'}]});await pending;
  assert.equal(t.list.dataset.preview,'1');
  assert.equal(t.list.innerHTML,'<article>1</article><article>2</article>');
  assert(t.hint.textContent.includes('完整片库'));
  console.log('PASS first-screen cards render ahead of full JSON, without enabling full-data state');
  t=fixture();const stale=t.ctx.showMappingFirstScreen(4);
  t.ctx.embyLibraryGeneration=5;
  t.respond({status:'success',series:[{id:'stale'}]});await stale;
  assert.equal(t.list.innerHTML,'initial');
  console.log('PASS late response from replaced generation cannot overwrite new content');
  t=fixture();const filtered=t.ctx.showMappingFirstScreen(4);
  t.ctx.embyFilterState.scope='share';
  t.respond({status:'success',series:[{id:'1'}]});await filtered;
  assert.equal(t.list.innerHTML,'initial');
  console.log('PASS preview never masquerades as a filtered full-library response');
  t=fixture();const ready=t.ctx.showMappingFirstScreen(4);
  t.ctx.embyLoaded=true;
  t.respond({status:'success',series:[{id:'old'}]});await ready;
  assert.equal(t.list.innerHTML,'initial');
  console.log('PASS completed full load wins over late preview');
  // The Python/JS sort may differ for titles with equal status/difference.
  // The completed list must re-order the existing image nodes rather than reload.
  t=fixture();
  const node=id=>{
    const img={getAttribute:()=>'/api/tmdb/poster/'+id+'.jpg'};
    const info={innerHTML:'old-'+id};
    const badge={className:'status',textContent:'old'};
    return {id, className:'poster-card',
      getAttribute:()=>id,
      querySelector:q=>q==='img'?img:q==='.info'?info:q==='.badge-status'?badge:null};
  };
  const oldOne=node('1'),oldTwo=node('2');
  t.list.dataset.preview='1';
  t.list.querySelectorAll=()=>[oldTwo,oldOne];
  t.list.replaceChildren=fragment=>{t.list.ordered=fragment.items;};
  t.ctx.document={
    createElement:()=>{
      const holder={};
      Object.defineProperty(holder,'innerHTML',{set(html){
        const ids=Array.from(html.matchAll(/data-series-id="([^"]+)"/g),m=>m[1]);
        holder.cards=ids.map(node);
      }});
      holder.querySelectorAll=()=>holder.cards;
      return holder;
    },
    createDocumentFragment:()=>({items:[],appendChild(card){this.items.push(card);}})
  };
  // An actual browser invokes these functions with document in global scope.
  t.ctx.replaceMappingCardsStable(t.list,[{id:'1'},{id:'2'}],x=>'<div data-series-id="'+x.id+'"></div>','series');
  assert.strictEqual(t.list.ordered[0],oldOne);
  assert.strictEqual(t.list.ordered[1],oldTwo);
  assert.equal(t.list.dataset.preview,undefined);
  t.ctx.replaceMappingCardsStable(t.list,[{id:'2'},{id:'1'}],x=>'<div data-series-id="'+x.id+'"></div>','series');
  assert.strictEqual(t.list.ordered[0],oldTwo);
  assert.strictEqual(t.list.ordered[1],oldOne);
  console.log('PASS authoritative full sort keeps already decoded preview images');
  console.log('PASS 5/5 preview UI checks');
})().catch(e=>{console.error(e);process.exitCode=1;});
