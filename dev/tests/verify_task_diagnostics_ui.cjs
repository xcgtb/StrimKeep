// Shipped handlers: visible detail dialog, read-only diagnostic pages and log navigation.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[];
function node(id){return nodes[id]||(nodes[id]={dataset:{},value:'',textContent:'',innerHTML:'',hidden:false,open:false,children:[],classList:{toggle(){},add(){},remove(){},contains(){return false;}},setAttribute(){},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},addEventListener(k,fn){events[id+':'+k]=fn;},querySelectorAll(){return [];},querySelector(){return null;},showModal(){this.open=true;},close(){this.open=false;}});}
const ctx={console,URLSearchParams,Promise,Object,Array,Number,String,JSON,Set,Date,Math,encodeURIComponent,
 esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 document:{getElementById:node,querySelectorAll(){return [];},createElement:()=>node('new-'+Math.random()),addEventListener(){}},
 setTimeout(){},clearTimeout(){},showModalConfirm:async()=>false,sessionStorage:{setItem(){}},
 fetch(url,opt){if(url==='/api/cloud/status')return Promise.resolve({ok:true,json:async()=>({accounts:[]})});if(url==='/api/cloud/settings')return Promise.resolve({ok:true,json:async()=>({settings:{}})});return new Promise(resolve=>requests.push({url,opt,resolve}));}};
ctx.window=ctx;ctx.__activeTab='cloud';vm.createContext(ctx);
const root=path.resolve(__dirname,'../..');vm.runInContext(fs.readFileSync(path.join(root,'static/js/cloud-workbench.js'),'utf8'),ctx);
async function flush(){for(let i=0;i<35;i++)await Promise.resolve();}
function answer(r,body,status=200){assert(r);r.resolve({ok:status<400,status,json:async()=>body});}
function click(action){events['tab-cloud:click']({target:{closest:q=>q==='[data-cwa]'?{dataset:{cwa:action}}:null}});}
function target(selector,dataset={}){return {target:{closest:q=>q===selector?{dataset}:null}};}
async function run(){
 await ctx.cloudWorkbench.load();ctx.cloudWorkbench.navigate('jobs');await flush();
 answer(requests.shift(),{items:[{id:'task',kind:'workflow',state:'needs_review',message:'跳过 1 项'}]});await flush();
 node('cw-job-list').onclick(target('[data-job]',{job:'task'}));await flush();const detail=requests.shift();
 assert(node('cw-job-dialog').open&&node('cw-job-summary').textContent.includes('正在读取'));
 click('jobs');await flush();answer(requests.shift(),{items:[{id:'task',kind:'workflow',state:'needs_review',updated:2}]});await flush();assert(!requests.length,'list polling cannot replace a pending user detail request');
 const item={category:'skipped',label:'识别或执行跳过',name:'<unsafe>.mkv',reason:'未识别到作品',stage:'影视识别',suggestion:'核对 TMDB 后重新预览',page:'organize',module:'run',file_id:'4',attention:true};
 answer(detail,{job:{id:'task',kind:'workflow',state:'needs_review',message:'跳过',result:{skipped:[{name:item.name,reason:item.reason}]},steps:[],diagnostics:{items:[item],total:43,offset:0,has_more:true,counts:{skipped:43}}}});await flush();
 const box=node('cw-job-summary').children.find(n=>n.className==='cw-diagnostics');assert(box&&box.innerHTML.includes('下一步')&&box.innerHTML.includes('核对 TMDB')&&box.innerHTML.includes('&lt;unsafe&gt;')&&!box.innerHTML.includes('<unsafe>'));
 box.onclick(target('[data-diagnostic-next]'));await flush();let next=requests.shift();assert(next.url.includes('offset=30')&&!next.opt.method);
 answer(next,{diagnostics:{items:[{...item,name:'下一页.mkv'}],total:43,offset:30,has_more:false,counts:{skipped:43}}});await flush();assert(box.innerHTML.includes('下一页.mkv'));
 node('cw-diagnostic-category').value='filtered';node('cw-diagnostic-query').value='sample';box.onclick(target('[data-diagnostic-search]'));await flush();next=requests.shift();assert(next.url.includes('category=filtered')&&next.url.includes('query=sample'));
 answer(next,{diagnostics:{items:[{...item,name:'sample.mkv',label:'规则过滤',category:'filtered',rule:'sample',attention:false}],total:1,offset:0,has_more:false,counts:{filtered:1}}});await flush();assert(box.innerHTML.includes('命中规则')&&box.innerHTML.includes('data-attention="false"'));
 click('closeJobDetail');assert(!node('cw-job-dialog').open);
 node('cw-job-list').onclick(target('[data-job]',{job:'task'}));await flush();answer(requests.shift(),{detail:'暂时不可用'},503);await flush();assert(node('cw-job-dialog').open&&node('cw-job-summary').textContent.includes('刷新详情重试'));
 click('closeJobDetail');
 // Exercise the actual runtime switcher, not a copy of its deferred-load logic.
 vm.runInContext(fs.readFileSync(path.join(root,'static/js/records.js'),'utf8'),ctx);
 const runtime=fs.readFileSync(path.join(root,'static/js/runtime.js'),'utf8');vm.runInContext(runtime.slice(runtime.indexOf('function switchTab(id){'),runtime.indexOf('/* 手机 Floating Dock')),ctx);
 ctx.$=node;ctx.MENU=[{id:'cloud',name:'我的网盘'}];ctx.groupOf=id=>id;ctx.scrollTo=()=>{};ctx.toggleSidebar=()=>{};ctx.stopLogCenter=()=>{};
 let resolveLoad,opened='';ctx.cloudWorkbench.load=()=>new Promise(resolve=>resolveLoad=resolve);ctx.cloudWorkbench.inspectJob=id=>opened=id;ctx.__activeTab='history';
 const id='a'.repeat(32);ctx.openRuntimeTaskDetail(id);assert(ctx.__activeTab==='cloud'&&!opened);resolveLoad(true);await flush();assert(opened===id);
 const evidence=ctx.runtimeLogDetails({details:{job_id:id,reason:'未识别',suggestion:'核对作品',rule:'sample'}});assert(evidence.includes('查看任务详情')&&evidence.includes('openRuntimeTaskDetail')&&evidence.includes('处理建议')&&evidence.includes('原因：'));
 console.log('PASS visible task dialog, pending-read race, diagnostic pagination/filter/escaping, retry message, read-only requests and real log-to-task switcher');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
