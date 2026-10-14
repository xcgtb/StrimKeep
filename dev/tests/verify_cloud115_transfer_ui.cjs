const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},calls=[];
function node(id){return nodes[id]||(nodes[id]={value:'',innerHTML:'',textContent:'',disabled:false,hidden:false,checked:false,setAttribute(k,v){this[k]=v;}});}
function esc(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function pending(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
let reply=async()=>({accounts:[{id:'account',uid:'123',name:'115',enabled:true}],bindings:[],deletions:[],organize_plans:[],transfer_plans:[]});
const ctx={$:node,esc,jsarg:s=>esc(JSON.stringify(s)),console,document:{hidden:false},setTimeout(){},toast(){},
 openModal(html){node('modal').innerHTML=html;},api:async(url,options)=>{calls.push({url,options});return reply(url,options);}};
vm.createContext(ctx);
for(const name of ['cloud115.js','cloud115-transfer.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/',name),'utf8'),ctx);
(async()=>{
 assert.equal(calls.length,0,'script loading never probes account or share');
 node('cloud115Account').value='account';ctx.cloud115View.account='account';
 node('cloud115Cookie').value='UID=123; CID=fixture; SEID=fixture';
 await ctx.cloud115SaveCookie();assert.equal(node('cloud115Cookie').value,'');
 ctx.cloud115ShareView={generation:1,account:'account',handle:'handle',parent:'0',next:null,items:[{id:'100',name:'<unsafe>.mkv',is_dir:false}]};
 ctx.cloud115ShareChoose(0);assert(!node('modal').innerHTML.includes('<unsafe>'));
 node('cloud115TransferParent').value='2';node('cloud115TransferSidecars').value='101';
 const submitted=pending();reply=url=>url.endsWith('/transfer/preview')?submitted.promise:Promise.resolve({status:'running'});
 const first=ctx.cloud115PreviewTransfer();await ctx.cloud115PreviewTransfer();
 ctx.cloud115ShareView.items=[{id:'999',name:'later',is_dir:false}];submitted.resolve({task_id:'preview-job'});await first;
 const previews=calls.filter(x=>x.url.endsWith('/transfer/preview'));assert.equal(previews.length,1);
 const body=JSON.parse(previews[0].options.body);assert.equal(body.file_id,'100');assert.equal(body.handle,'handle');assert.deepEqual(body.sidecar_ids,['101']);
 assert.equal(calls.filter(x=>x.url.endsWith('/execute')).length,0,'preview never submits transfer');
 const plan={plan_id:'plan',account_id:'account',state:'preview',target_parent:'2',folder_name:'<folder>',files:[{source_id:'100',name:'<unsafe>.mkv'}],targets:[],mkdir_state:'pending',receive_state:'pending'};
 ctx.cloud115RenderTransfer(plan);assert(!node('cloud115TransferPreview').innerHTML.includes('<folder>'));assert.equal(node('cloud115TransferBtn').onclick,'cloud115ExecuteTransfer()');
 const execute=pending();reply=url=>url.endsWith('/execute')?execute.promise:Promise.resolve({status:'running'});
 const accepted=ctx.cloud115ExecuteTransfer();await ctx.cloud115ExecuteTransfer();execute.resolve({task_id:'execute-job'});await accepted;
 const writes=calls.filter(x=>x.url.endsWith('/execute'));assert.equal(writes.length,1);assert.equal(JSON.parse(writes[0].options.body).confirm,true);
 const obsolete=pending();reply=()=>obsolete.promise;const old=ctx.cloud115WatchTransfer('execute-job','execute');
 ctx.cloud115TransferSelection={task:'new'};node('cloud115TransferResult').textContent='new result';obsolete.resolve({status:'success',result:plan});await old;
 assert.equal(node('cloud115TransferResult').textContent,'new result');
 const done={...plan,state:'done',targets:[{source_id:'100',file_id:'600',name:'video.mkv'},{source_id:'101',file_id:'601',name:'subtitle.ass'}]};
 ctx.cloud115TransferSelection={result:done};reply=async()=>({file:{id:'600',name:'video.mkv',parent_id:'500',is_dir:false}});
 await ctx.cloud115UseTransferTargets('strm');assert.equal(ctx.cloud115Selection.file.id,'600');assert.equal(node('cloud115Sidecars').value,'601');
 assert(calls.at(-1).url.endsWith('/files/600'),'own target ID is read independently of source ID');
 console.log('PASS 115 transfer UI: no startup probes; Cookie clearing; escaped previews; pinned source IDs; explicit single confirmation; stale task isolation; owned-ID STRM handoff');
})().catch(e=>{console.error(e);process.exitCode=1;});
