// Shipped task UI: response ordering, stages, filters and bounded result DOM.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[];
function node(id){return nodes[id]||(nodes[id]={dataset:{},value:'',textContent:'',innerHTML:'',hidden:false,children:[],setAttribute(){},classList:{toggle(){}},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},addEventListener(k,fn){events[id+':'+k]=fn;},querySelectorAll(){return [];}});}
const ctx={console,URLSearchParams,Promise,Object,Array,Number,String,JSON,encodeURIComponent,esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),document:{getElementById:node,createElement:()=>node('created-'+Math.random())},setTimeout(){},clearTimeout(){},showModalConfirm:async()=>false,fetch(url,opt){
 if(url==='/api/cloud/status')return Promise.resolve({ok:true,json:async()=>({accounts:[{id:'A',name:'甲'},{id:'B',name:'乙'}]})});
 if(url.endsWith('/directories'))return Promise.resolve({ok:true,json:async()=>({directories:{},revision:0})});
 if(url==='/api/cloud/settings')return Promise.resolve({ok:true,json:async()=>({settings:{}})});
 return new Promise(resolve=>requests.push({url,opt,resolve}));
}};ctx.window=ctx;ctx.__activeTab='cloud';vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/cloud-workbench.js'),'utf8'),ctx);
async function flush(){for(let i=0;i<40;i++)await Promise.resolve();}
function answer(request,body,status=200){assert(request);request.resolve({ok:status<400,status,json:async()=>body});}
function click(action){events['tab-cloud:click']({target:{closest:q=>q==='[data-cwa]'?{dataset:{cwa:action}}:null}});}
function taskClick(selector,dataset){node('cw-job-list').onclick({target:{closest:q=>q===selector?{dataset}:null}});}
async function run(){
 await ctx.cloudWorkbench.load();ctx.cloudWorkbench.navigate('jobs');await flush();const old=requests.shift();
 click('jobs');await flush();const current=requests.shift();
 const followup={id:'parent',kind:'organize',state:'success',message:'后续排队',activity:{label:'STRM 后续处理中',active:true,needs_attention:false,publication:{verified:2,queued:1},notification:{verified:1},deferred:{queued:1}}};
 answer(current,{items:[followup,{id:'unknown',kind:'sync',state:'needs_review',message:'<unsafe>',activity:{label:'后续结果待核实',active:false,needs_attention:true,notification:{unknown:1}}}]});await flush();
 answer(old,{items:[{id:'stale',kind:'sync',state:'success',message:'过时响应'}]});await flush();
 assert(node('cw-job-list').innerHTML.includes('STRM 后续处理中')&&!node('cw-job-list').innerHTML.includes('过时响应'));
 assert(node('cw-job-list').innerHTML.includes('&lt;unsafe>')&&!node('cw-job-list').innerHTML.includes('<unsafe>'));
 taskClick('[data-job-filter]',{jobFilter:'attention'});assert(!node('cw-job-list').innerHTML.includes('data-job="parent"')&&node('cw-job-list').innerHTML.includes('data-job="unknown"'));
 taskClick('[data-job-filter]',{jobFilter:'active'});assert(node('cw-job-list').innerHTML.includes('data-job="parent"')&&!node('cw-job-list').innerHTML.includes('data-job="unknown"'));
 taskClick('[data-job]',{job:'parent'});await flush();let first=requests.shift();
 taskClick('[data-job]',{job:'parent'});await flush();let second=requests.shift();
 answer(second,{job:{...followup,message:'新详情',steps:[],result:{files:Array.from({length:5000},(_,i)=>({name:'File-'+i})),errors:[{name:'问题文件',reason:'识别不明确'}]}}});await flush();
 answer(first,{job:{...followup,message:'旧详情',result:{}}});await flush();
 assert(node('cw-job-summary').children.length<45,'thousands of files cannot create thousands of summary nodes');
 assert(node('cw-job-summary').children.some(n=>n.textContent.includes('新详情'))&&!node('cw-job-summary').children.some(n=>n.textContent.includes('旧详情')));
 assert(node('cw-job-summary').children[1].className==='cw-task-issues','issues before file results');
 click('jobs');await flush();answer(requests.shift(),{detail:'暂时不可用'},503);await flush();assert(node('cw-job-list').innerHTML.includes('data-job="parent"'));assert(node('cw-job-status').textContent.includes('予以保留'));
 taskClick('[data-job]',{job:'parent'});await flush();const accountOld=requests.shift();
 node('cw-account').value='B';node('cw-account').onchange();await flush();answer(requests.shift(),{items:[]});await flush();
 answer(accountOld,{job:{...followup,message:'旧账号详情',result:{}}});await flush();assert(!node('cw-job-summary').children.some(n=>n.textContent.includes('旧账号详情')));
 console.log('PASS task stages, scoped filters, same-job stale responses, account reset, escaped output, bounded detail and retained records on failure');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
