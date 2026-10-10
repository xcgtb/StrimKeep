// Actual overview/status logic with temporary DOM and response substitutes.
const vm=require('vm'),fs=require('fs'),path=require('path'),assert=require('assert');
const nodes={},events={},pending=[],timers=new Map();let seq=0;
function node(){return {textContent:'',innerHTML:'',style:{width:'',setProperty(){}},classList:{toggle(){}},attrs:{},setAttribute(k,v){this.attrs[k]=v;}};}
const ctx=vm.createContext({console,Date,Number,String,Object,Array,Math,JSON,Promise,Set,
 $:id=>nodes[id]||(nodes[id]=node()),document:{hidden:false,addEventListener:(name,fn)=>events[name]=fn},
 api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
 clearTimeout:id=>timers.delete(id),setTimeout:(fn,ms)=>{timers.set(++seq,{fn,ms});return seq;}});
ctx.window=ctx;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/dashboard.js'),'utf8'),ctx);
(async()=>{
 ctx.renderDashboard({localCount:'18,139',shareCount:'140,919'});
 assert(nodes['stat-share'].textContent==='140,919'&&nodes['stat-local'].textContent==='18,139');
 ctx.renderDashboard({localCount:0,shareCount:0});assert(nodes['stat-local'].textContent===0&&nodes['stat-share'].textContent===0);
 console.log('PASS thousands-formatted and explicit zero STRM counts display accurately');
 ctx.renderRuntimeStatus({ts:100,version:'dev',bot:{state:'degraded'},task:{kind:'inter_check'},storage:{issues:[{}]}});
 assert(nodes.sideBot.textContent==='连接重试中'&&nodes.sideStorage.attrs['data-tone']==='bad');
 assert(nodes.sideTask.textContent==='双库扫描'&&nodes.sideTask.attrs['data-tone']==='running');
 ctx.renderRuntimeStatus({ts:101,bot:{state:'ok'},comparison:{running:true,percent:48},storage:{issues:[]}});
 assert(nodes.sideTask.textContent==='片库对照 48%'&&nodes.sideBot.textContent==='轮询正常');
 console.log('PASS backend task, bot degradation, comparison progress and storage errors have distinct labels');
 let task=ctx.pollRuntimeStatus();assert(pending[0].url==='/api/runtime/status');pending.shift().reject(Error('offline'));await task;
 assert(nodes.headerRuntime.textContent==='连接中断'&&nodes.sideTask.textContent==='状态待更新');
 assert(timers.size===1);
 console.log('PASS a failed status request marks prior values as stale and schedules one retry');
 ctx.document.hidden=true;events.visibilitychange();assert(!timers.size);await ctx.pollRuntimeStatus();assert(!pending.length);
 console.log('PASS hidden pages suspend the lightweight status loop');
 const html=fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8');
 assert(html.includes('<title>StrimKeep</title>'));
 assert(html.includes('cinema-governance') && html.includes('id="govScanBtn"'));
 assert(html.includes('cinema-workspace') && html.includes('cinema-main-column') && html.includes('cinema-side-column'));
 assert(html.includes('<details id="govTruth"')&&html.includes('id="sideStorage"'));
 console.log('PASS the product brand, overview priority and expandable governance evidence are present');
 console.log('PASS 5/5; real shipped handlers with DOM substitutes only');
})().catch(e=>{console.error(e);process.exitCode=1;});
