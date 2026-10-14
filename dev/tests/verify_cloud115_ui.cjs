const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/cloud115.js'),'utf8');
const nodes={},calls=[];
function node(id){return nodes[id]||(nodes[id]={value:'',textContent:'',innerHTML:'',checked:false,disabled:false,hidden:false,setAttribute(k,v){this[k]=v;}});}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
let response=async()=>({accounts:[],bindings:[],deletions:[]});
const ctx={$:node,esc,jsarg:s=>esc(JSON.stringify(String(s))),encodeURIComponent,console,document:{hidden:false},setTimeout(){},toast(){},
 openModal(html){node('modal').innerHTML=html;},api:async(url,options)=>{calls.push({url,options});return response(url,options);}};
vm.createContext(ctx);vm.runInContext(source,ctx);
function pending(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
(async()=>{
 assert.equal(calls.length,0,'loading scripts must not connect to 115');
 response=async()=>({accounts:[{id:'account',uid:'123',name:'<unsafe>',enabled:true}],bindings:[],deletions:[],require_binding:true,playback_base:'http://nas'});
 await ctx.loadCloud115();assert(node('cloud115Strict').checked);assert(!node('cloud115Account').innerHTML.includes('<unsafe>'));
 node('cloud115Access').value='secret-access';node('cloud115Refresh').value='secret-refresh';
 response=async url=>url.endsWith('/accounts')?{}:{accounts:[],bindings:[],deletions:[]};
 await ctx.cloud115SaveAccount();assert.equal(node('cloud115Access').value,'');assert.equal(node('cloud115Refresh').value,'');
 node('cloud115Account').value='account';ctx.cloud115ResetBrowser();
 const old=pending();response=()=>old.promise;
 const browse=ctx.cloud115Files('0',0);
 node('cloud115Account').value='other';ctx.cloud115ResetBrowser();
 old.resolve({items:[{id:'10',name:'obsolete',is_dir:false}],next_offset:null});await browse;
 assert.equal(node('cloud115Files').innerHTML,'','old account response is ignored');
 node('cloud115Account').value='account';ctx.cloud115ResetBrowser();
 response=async()=>({items:[{id:'10',name:'<unsafe>.mkv',is_dir:false}],next_offset:null});
 await ctx.cloud115Files('0',0);assert(!node('cloud115Files').innerHTML.includes('<unsafe>'));
 ctx.cloud115Choose(0);assert(!node('modal').innerHTML.includes('<unsafe>'));
 node('cloud115Relative').value='电影/样本/video.strm';node('cloud115Sidecars').value='11,12';
 // The selected remote identity is frozen, independent of later browser rows.
 ctx.cloud115View.items=[{id:'99',name:'another.mkv',is_dir:false}];
 const submitted=pending();response=url=>url.endsWith('/generate')?submitted.promise:Promise.resolve({status:'running'});
 const first=ctx.cloud115Generate(0);await ctx.cloud115Generate(0);
 submitted.resolve({task_id:'job'});await first;
 const writes=calls.filter(x=>x.url.endsWith('/generate'));assert.equal(writes.length,1);
 const body=JSON.parse(writes[0].options.body);assert.equal(body.file_id,'10');assert.deepEqual(body.sidecar_ids,['11','12']);
 const poll=pending();response=()=>poll.promise;const waiting=ctx.cloud115WatchTask('job');
 ctx.cloud115Selection={file:{id:'99'},account:'other',task:'new-job'};node('cloud115GenerateResult').textContent='new';
 poll.resolve({status:'success'});await waiting;assert.equal(node('cloud115GenerateResult').textContent,'new');
 // Device polling is a CSRF-protected POST and hides QR after success.
 node('cloud115AuthPanel').open=true;ctx.cloud115QrGeneration=4;
 response=async()=>({state:'waiting'});
 await ctx.cloud115PollQr('session',4,Date.now()/1000+100);
 assert.equal(calls.at(-1).options.method,'POST');
 const count=calls.length;ctx.document.hidden=true;
 await ctx.cloud115PollQr('session',4,Date.now()/1000+100);assert.equal(calls.length,count);
 ctx.document.hidden=false;node('cloud115Qr').innerHTML='old qr';
 await ctx.cloud115PollQr('session',4,Date.now()/1000-1);assert.equal(node('cloud115Qr').innerHTML,'');
 const qr=pending();response=()=>qr.promise;
 const stale=ctx.cloud115PollQr('session',4,Date.now()/1000+100);
 ctx.cloud115QrGeneration=5;node('cloud115QrStatus').textContent='new-session';
 qr.resolve({state:'done'});await stale;assert.equal(node('cloud115QrStatus').textContent,'new-session');
 ctx.cloud115ReportedTasks.cloud115AdoptResult='adopt';
 response=async url=>url.includes('/api/task/')?{status:'success',result:{message:'身份核验完成'}}:{accounts:[],bindings:[],deletions:[]};
 await ctx.cloud115ReportTask('adopt','cloud115AdoptResult','cloud115AdoptBtn');
 assert.equal(node('cloud115AdoptResult').textContent,'身份核验完成');assert.equal(node('cloud115AdoptBtn').disabled,false);
 ctx.cloud115View={account:'account',parent:'1',items:[{id:'10',name:'<name>.mkv',is_dir:false}],generation:0};
 ctx.cloud115OrganizeChoose(0);assert(!node('modal').innerHTML.includes('<name>'));
 node('cloud115OrganizeName').value='标准.mkv';node('cloud115OrganizeParent').value='2';node('cloud115OrganizeSidecars').value='11 | 标准.ass';
 const previewWrite=pending();response=url=>url.endsWith('/organize/preview')?previewWrite.promise:Promise.resolve({status:'running'});
 const previewing=ctx.cloud115OrganizePreview();await ctx.cloud115OrganizePreview();
 previewWrite.resolve({task_id:'preview-job'});await previewing;
 const previewWrites=calls.filter(x=>x.url.endsWith('/organize/preview'));assert.equal(previewWrites.length,1);
 const previewBody=JSON.parse(previewWrites[0].options.body);assert.equal(previewBody.file_id,'10');assert.equal(previewBody.sidecars[0].new_name,'标准.ass');
 assert.equal(calls.filter(x=>x.url.endsWith('/execute')).length,0,'preview must not submit a remote write');
 ctx.cloud115RenderOrganizePlan({plan_id:'plan',files:[{file_id:'10',old_name:'<old>',new_name:'<new>',old_parent:'1',new_parent:'2'}]});
 assert(!node('cloud115OrganizePreview').innerHTML.includes('<old>'));assert.equal(node('cloud115OrganizeBtn').onclick,'cloud115OrganizeExecute()');
 const executeWrite=pending();response=url=>url.endsWith('/execute')?executeWrite.promise:Promise.resolve({status:'running'});
 const executing=ctx.cloud115OrganizeExecute();await ctx.cloud115OrganizeExecute();executeWrite.resolve({task_id:'execute-job'});await executing;
 const executes=calls.filter(x=>x.url.endsWith('/execute'));assert.equal(executes.length,1);assert.equal(JSON.parse(executes[0].options.body).confirm,true);
 const obsolete=pending();response=()=>obsolete.promise;const oldTask=ctx.cloud115WatchOrganize('execute-job','execute');
 ctx.cloud115OrganizeSelection={task:'other'};node('cloud115OrganizeResult').textContent='other result';obsolete.resolve({status:'success',result:{message:'old result'}});await oldTask;
 assert.equal(node('cloud115OrganizeResult').textContent,'other result');
 console.log('PASS 115 UI: no automatic cloud requests; token clearing; stale account/task isolation; escaped names; frozen identities; duplicate submit blocked');
})().catch(e=>{console.error(e);process.exitCode=1;});
