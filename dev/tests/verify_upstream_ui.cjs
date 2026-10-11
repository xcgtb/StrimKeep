const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'../..');
const source=fs.readFileSync(path.join(root,'static/js/upstream.js'),'utf8');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const runtime=fs.readFileSync(path.join(root,'static/js/runtime.js'),'utf8');
const nodes={},calls=[],timers=[];
function node(id){return nodes[id]||(nodes[id]={value:'',checked:false,disabled:false,textContent:'',innerHTML:'',
 querySelectorAll(){return [];},querySelector(){return null;},classList:{contains(){return true;}}});}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
let accept=true,request=async()=>({status:'success',settings:{url:'http://fixture',enabled:true,has_token:true,max_points:0}});
const ctx={$:node,esc,jsarg:s=>esc(JSON.stringify(String(s))),encodeURIComponent,Uint32Array,
 window:{crypto:{getRandomValues(values){values.fill(7);}}},confirm:()=>accept,toast(){},
 setTimeout:fn=>timers.push(fn),api:async(url,opts)=>{calls.push({url,opts});return request(url,opts);},
 openModal(markup){node('modalBody').innerHTML=markup;node('modalBody').querySelector=()=>({getAttribute:()=>String(ctx.tgtoGeneration)});}};
vm.createContext(ctx);vm.runInContext(source,ctx);
assert.equal(calls.length,0,'evaluating the feature must not query upstream');
assert(html.includes('id="tgtoToken" type="password"'));
assert(runtime.includes("'conn','conn','conn','conn'"),'new settings panel must not displace existing connection panels');
for(const media_type of ['movie','tv']) {
 const markup=ctx.tgtoButton({title:'<img onerror=evil()>',type:media_type,tmdb_id:'7',year:2026});
 assert(!markup.includes('<img') && markup.includes('event.stopPropagation()'));
 ctx.tgtoOpen({title:'<img onerror=evil()>',media_type,tmdb_id:'7',year:2026});
 assert(!node('modalBody').innerHTML.includes('<img'));
 assert.equal(node('modalBody').innerHTML.includes('id="tgtoSeason"'),media_type==='tv');
}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
(async()=>{
 await ctx.loadTgtoConfig();
 assert.equal(node('tgtoToken').value,'');assert(node('tgtoToken').placeholder.includes('已保存'));
 calls.length=0;
 ctx.tgtoOpen({title:'样本',media_type:'tv',tmdb_id:'7',year:2026});
 node('tgtoSeason').value='1';node('tgtoEpisode').value='2';
 const pending=deferred();request=()=>pending.promise;
 const search=ctx.tgtoSearch();await ctx.tgtoSearch();
 assert.equal(calls.length,1,'duplicate search is blocked');
 const body=JSON.parse(calls[0].opts.body);assert.equal(body.season,'1');assert.equal(body.episode,'2');
 ctx.tgtoOpen({title:'另一部',media_type:'tv'});
 pending.resolve({items:[{title:'old',handle:'h',transferable:true}],message:'old'});await search;
 assert.equal(node('tgtoResults').innerHTML,'','obsolete search cannot overwrite another title');
 request=async()=>({items:[{title:'<script>evil</script>',source:'s',handle:'h',transferable:true}],message:'候选'});
 await ctx.tgtoSearch();
 assert(!node('tgtoResults').innerHTML.includes('<script>'));
 calls.length=0;accept=false;
 request=async()=>({settings:{max_points:0}});
 await ctx.tgtoTransfer(0);
 assert(!calls.some(c=>c.url==='/api/upstream/transfer'),'cancelled transfer performs no write');
 accept=true;node('tgtoProvider').value='115';calls.length=0;
 request=async(url)=>{
  if(url.endsWith('/config'))return {settings:{max_points:0}};
  if(url.endsWith('/transfer'))throw Error('timeout');
  throw Error('unexpected');
 };
 await ctx.tgtoTransfer(0);
 const id=ctx.tgtoView.items[0].operation;
 assert(node('tgtoStatus').textContent.includes('未确认'));
 await ctx.tgtoTransfer(0);
 assert.equal(ctx.tgtoView.items[0].operation,id,'uncertain manual repeat retains request identity');
 assert.equal(calls.filter(c=>c.url.endsWith('/transfer')).length,2,'no automatic retry');
 const view=ctx.tgtoView;view.mode='jobs';
 request=async()=>({items:[{id:'one',title:'样本',provider:'115',state:'submitting',message:'提交中'}]});
 await ctx.tgtoJobs(view);assert(timers.length>0);
 view.mode='history';calls.length=0;timers.pop()();await Promise.resolve();
 assert.equal(calls.length,0,'task polling stops after switching to history');
 ctx.tgtoOpen({title:'资源筛选',media_type:'movie',poster:'javascript:evil()'});
 assert(!node('modalBody').innerHTML.includes('javascript:'));
 ctx.tgtoView.searched=true;
 ctx.tgtoView.items=[{title:'A',source:'RED',resolution:'2160p',handle:'a',transferable:true},
  {title:'B',source:'115',resolution:'1080p',handle:'b',transferable:true}];
 ctx.tgtoRenderResources(ctx.tgtoView);
 ctx.tgtoSelectFacet('source',ctx.tgtoView.facets.source.indexOf('115'));
 assert(node('tgtoResults').innerHTML.includes('tgtoTransfer(1)'),'filter preserves original candidate index');
 assert(!node('tgtoResults').innerHTML.includes('tgtoTransfer(0)'));
 assert.equal(calls.length,0,'local facets never call upstream');
 const targetWait=deferred();request=()=>targetWait.promise;
 const targets=ctx.tgtoLoadTargets(ctx.tgtoView);
 ctx.tgtoOpen({title:'新作品',media_type:'movie'});
 node('tgtoTarget').innerHTML='current';
 targetWait.resolve({items:[{id:'115',name:'old'}]});await targets;
 assert.equal(node('tgtoTarget').innerHTML,'current','stale target reply cannot affect new view');
 request=async()=>({fields:{title:'<script>x</script>',target_path:'/media/name'}});
 await ctx.tgtoDetail('7');assert(!node('tgtoResults').innerHTML.includes('<script>'));
 console.log('PASS upstream UI: no startup request, secret preservation, safe rendering, scoped search, stale replies, confirmation, uncertain outcome and scoped task polling');
})().catch(e=>{console.error(e);process.exitCode=1;});
