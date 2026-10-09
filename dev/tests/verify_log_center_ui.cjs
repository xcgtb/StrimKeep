// Actual shipped handlers with a DOM substitute; no browser visual claim.
const vm=require('vm'),fs=require('fs'),assert=require('assert'),path=require('path');
const events={},timers=new Map(),requests=[],nodes={};let seq=0;
function node(){return {textContent:'',innerHTML:'',children:[],scrollHeight:100,scrollTop:0,clientHeight:100,
 querySelector(){return this.children[0]||null;},
 insertAdjacentHTML(_,html){const id=html.match(/data-log-id="([^"]+)"/)[1],box=this;
  this.children.push({html,getAttribute(){return id;},remove(){box.children.shift();}});},
 get firstElementChild(){return this.children[0];}};}
for(const name of ['recordsList','logsStatus','logPause','archiveList'])nodes[name]=node();
const ctx=vm.createContext({console,Set,Date,Math,Number,String,Object,Array,Promise,JSON,
 $:id=>nodes[id],esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 api:url=>new Promise(resolve=>requests.push({url,resolve})),
 setTimeout:(fn,ms)=>{timers.set(++seq,{fn,ms});return seq;},clearTimeout:id=>timers.delete(id),
 document:{hidden:false,addEventListener:(name,fn)=>events[name]=fn,getElementById:id=>nodes[id]}});
ctx.window=ctx;ctx.__activeTab='history';ctx.location={};
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/records.js'),'utf8'),ctx);
const row=id=>({id,ts:'2026-10-09T12:00:00',level:'INFO',source:'bot',message:'line <script>\nsecond'});
async function respond(task,rows,cursor){const req=requests.shift();req.resolve({logs:rows,cursor,degraded:false});await task;return req;}
(async()=>{
 let req=await respond(ctx.loadRecords(),[row(1)],1);
 assert(req.url.endsWith('after=0'));assert(nodes.recordsList.children.length===1);
 assert(nodes.recordsList.children[0].html.includes('&lt;script&gt;')&&!nodes.recordsList.children[0].html.includes('<script>'));
 req=await respond(ctx.pollLogCenter(),[row(1),row(2)],2);
 assert(req.url.endsWith('after=1')&&nodes.recordsList.children.length===2&&ctx.logCenter.cursor===2);
 console.log('PASS incremental append keeps earlier rows, deduplicates and escapes multiline logs');
 await respond(ctx.pollLogCenter(),Array.from({length:510},(_,i)=>row(i+3)),512);
 assert(nodes.recordsList.children.length===500&&ctx.logCenter.ids.size===500);
 console.log('PASS the live viewport retains at most 500 rows');
 ctx.toggleLogPause();assert(ctx.logCenter.paused&&timers.size===0);
 await ctx.pollLogCenter();assert(!requests.length);
 await respond(ctx.loadRecords(),[row(513)],513);assert(timers.size===0);
 console.log('PASS pause stops automatic polling while a manual refresh still works');
 ctx.logCenter.paused=false;let task=ctx.pollLogCenter(),pending=requests.shift();ctx.__activeTab='plans';ctx.stopLogCenter();
 pending.resolve({logs:[row(514)],cursor:514});await task;
 assert(ctx.logCenter.cursor===513&&!timers.size);
 console.log('PASS navigation discards late replies and stops the live poll');
 ctx.__activeTab='history';ctx.scheduleLogPoll();assert(timers.size===1);
 ctx.document.hidden=true;events.visibilitychange();assert(!timers.size);
 await ctx.pollLogCenter();assert(!requests.length);ctx.document.hidden=false;
 console.log('PASS hidden pages suspend polling');
 ctx.renderCleanupArchives([{id:1,category:'目录清理',title:'完成',ts:'2026-10-09 12:00:00',details:['20 subtitles']}]);
 assert(nodes.archiveList.innerHTML.includes('完成')&&ctx.__recs.length===1);
 assert(fs.readFileSync(path.join(__dirname,'../../static/js/plans.js'),'utf8').includes('renderCleanupArchives'));
 const html=fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8');
 assert(html.includes('id="archiveList"')&&html.includes('日志中心')&&html.includes('暂停自动刷新'));
 console.log('PASS structured cleanup cards remain in plan archives beside the live log center');
 console.log('PASS 6/6; shipped handlers with DOM substitutes only');
})().catch(e=>{console.error(e);process.exitCode=1;});
