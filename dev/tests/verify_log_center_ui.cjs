// Actual shipped handlers with a DOM substitute; no browser visual claim.
const vm=require('vm'),fs=require('fs'),assert=require('assert'),path=require('path');
const events={},timers=new Map(),requests=[],nodes={};let seq=0;
function node(){return {textContent:'',innerHTML:'',children:[],scrollHeight:100,scrollTop:0,clientHeight:100,
 querySelector(){return this.children[0]||null;},
 insertAdjacentHTML(_,html){const id=html.match(/data-log-id="([^"]+)"/)[1],box=this;
  this.children.push({html,getAttribute(){return id;},remove(){box.children.shift();}});},
 get firstElementChild(){return this.children[0];}};}
for(const name of ['recordsList','logsStatus','logPause','archiveList','logSource','logQuery'])nodes[name]=node();
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
 // A filter change invalidates an earlier in-flight response and restarts its cursor.
 nodes.logSource.value='cloud_naming';nodes.logQuery.value='Movie';
 task=ctx.pollLogCenter();pending=requests.shift();ctx.filterRuntimeLogs();
 pending.resolve({logs:[row(900)],cursor:900});await task;
 assert(ctx.logCenter.cursor===0);assert(!ctx.logCenter.ids.has('900'));
 task=ctx.pollLogCenter();req=requests.shift();assert(req.url.includes('source=cloud_naming')&&req.url.includes('query=Movie')&&req.url.includes('after=0'));
 req.resolve({logs:[{...row(901),source:'cloud_naming',message:'Movie.mkv'}],cursor:901});await task;
 assert(nodes.recordsList.children.at(-1).html.includes('重命名'));
 console.log('PASS module/keyword filters discard stale replies, reset cursor and label cloud stages');
 const evidence=ctx.runtimeLogDetails({details:{job_id:'a'.repeat(32),status:'dispatched',file_id:'4',sha1:'B'.repeat(40),elapsed_ms:1234,source_name:'<script>secret</script>',cookie:'must-not-display'}});
 const counts=ctx.runtimeLogDetails({details:{event:'action_plan',status:'finished',directory_count:2,video_count:5,renamed_count:5,failed_count:0}});
 assert(counts.includes('动作规划')&&counts.includes('目标 / 子目录数')&&counts.includes('视频数')&&counts.includes('计划重命名数')&&counts.includes('异常数量'),'phase counters need readable Chinese labels');
 assert(evidence.includes('已发送，待核实')&&evidence.includes('1.23 秒')&&evidence.includes('115 文件 ID'));
 assert(evidence.includes('&lt;script&gt;')&&!evidence.includes('<script>')&&!evidence.includes('must-not-display'));
 assert(evidence.includes('focusRuntimeTask'));
 console.log('PASS structured evidence escapes values, distinguishes dispatch from verified results, and links tasks');
 console.log('PASS 8/8; shipped handlers with DOM substitutes only');
})().catch(e=>{console.error(e);process.exitCode=1;});
