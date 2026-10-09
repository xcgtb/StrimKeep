// Exercise the real hint, scan, and polling functions with a minimal DOM.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
function harness(){
  const nodes=new Map(),pending=[],toasts=[],timers=[];let renders=0;
  const node=id=>{
    if(!nodes.has(id)){
      const classes=new Set();nodes.set(id,{textContent:'',innerHTML:'',disabled:false,style:{},attrs:{},
        classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)},
        setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];}});
    }
    return nodes.get(id);
  };
  const context=vm.createContext({$:node,Date,Math,Promise,
    setTimeout:fn=>{timers.push(fn);return timers.length;},
    document:{body:node('body'),hidden:false,addEventListener(){},removeEventListener(){}},
    window:{addEventListener(){},removeEventListener(){}},
    api:(url,opts)=>new Promise((resolve,reject)=>pending.push({url,opts,resolve,reject})),
    toast:(...args)=>toasts.push(args),injectIcons(){},
    currentPlan:'old-plan',govScanTs:1,govData:{old:true},govPlanTtlMs:900000,
    renderGovList(){renders++;},saveGovStateToSession(){},loadGovTruth(){},tickGovBanner(){},setGovBanner(){}
  });
  for(const [start,end] of [['var __taskHintControl = null;', 'function toggleSidebar('],
                          ['async function pollTask(', 'function statusOk('],
                          ['async function scanLibrary(', 'function setGovFilter(']]){
    vm.runInContext(html.slice(html.indexOf(start),html.indexOf(end)),context);
  }
  return {context,node,pending,toasts,timers,renders:()=>renders};
}
async function flush(){for(let i=0;i<12;i++)await Promise.resolve();}
function request(h,url){const i=h.pending.findIndex(p=>p.url===url);assert(i>=0,'missing request '+url);return h.pending.splice(i,1)[0];}
async function main(){
  assert(!html.match(/<div class="task-hint"[^>]+>/)[0].includes('onclick'));
  assert(html.includes('id="taskHintClose" onclick="onTaskHintClose()"'));
  let h=harness(),ctx=h.context;
  let scan=ctx.scanLibrary();await ctx.scanLibrary();assert(h.pending.length===1);
  await ctx.onTaskHintClose();assert(h.node('taskHintText').textContent.includes('正在取消'));
  assert(h.node('taskHint').classList.contains('show') && h.node('govScanBtn').disabled);
  request(h,'/api/check').resolve({task_id:'early'});await flush();
  request(h,'/api/task/early/cancel').resolve({cancel_requested:true});
  request(h,'/api/task/early').resolve({status:'cancelled'});await scan;
  assert(ctx.currentPlan==='old-plan' && ctx.govData.old && h.renders()===0);
  assert(!h.node('govScanBtn').disabled && !h.node('taskHint').classList.contains('show'));
  assert(h.toasts.at(-1)[0].includes('已取消'));
  console.log('PASS X before task ID arrives cancels that scan and preserves prior facts');

  h=harness();ctx=h.context;scan=ctx.scanLibrary();request(h,'/api/check').resolve({task_id:'run'});await flush();
  const cancel=ctx.onTaskHintClose();await ctx.onTaskHintClose();
  assert(h.pending.filter(p=>p.url.endsWith('/cancel')).length===1);
  assert(h.node('taskHint').classList.contains('show') && h.node('scanBtn').disabled);
  request(h,'/api/task/run').resolve({status:'running'});await flush();
  request(h,'/api/task/run/cancel').resolve({cancel_requested:true});await cancel;await flush();
  assert(h.node('govScanBtn').disabled); // request accepted is not worker completion
  request(h,'/api/task/run').resolve({status:'cancelled'});await scan;
  assert(!h.node('govScanBtn').disabled);
  const retry=ctx.scanLibrary();request(h,'/api/check').resolve({task_id:'retry'});await flush();
  request(h,'/api/task/retry').resolve({status:'cancelled'});await retry;
  console.log('PASS repeated X sends one request and buttons unlock only after worker exit; retry works');

  h=harness();ctx=h.context;scan=ctx.scanLibrary();request(h,'/api/check').resolve({task_id:'network'});await flush();
  let attempt=ctx.onTaskHintClose();request(h,'/api/task/network/cancel').reject(new Error('请求超时'));await attempt;
  assert(h.toasts.at(-1)[0].includes('取消未确认') && h.node('scanBtn').disabled);
  assert(!h.node('taskHintClose').disabled && h.node('taskHint').classList.contains('show'));
  attempt=ctx.onTaskHintClose();request(h,'/api/task/network/cancel').resolve({cancel_requested:true});await attempt;
  request(h,'/api/task/network').resolve({status:'cancelled'});await scan;
  console.log('PASS failed cancel request keeps polling and permits retry without a false cancelled result');

  h=harness();ctx=h.context;scan=ctx.scanLibrary();request(h,'/api/check').resolve({task_id:'done'});await flush();
  attempt=ctx.onTaskHintClose();request(h,'/api/task/done/cancel').resolve({cancel_requested:false});await attempt;
  request(h,'/api/task/done').resolve({status:'success',result:{plan_id:'new-plan',total_clean_cnt:2}});await scan;
  assert(ctx.currentPlan==='new-plan' && h.renders()===1 && h.toasts.at(-1)[0].includes('扫描完成'));
  console.log('PASS completion winning the race displays successful result instead of false cancellation');

  h=harness();ctx=h.context;const cleanup=ctx.pollTask('clean','执行清理中…');await flush();
  await ctx.onTaskHintClose();assert(!h.node('taskHint').classList.contains('show'));
  assert(!h.pending.some(p=>p.url.endsWith('/cancel')));
  request(h,'/api/task/clean').resolve({status:'success'});await cleanup;
  console.log('PASS destructive cleanup X only dismisses its hint and never requests scan cancellation');
  console.log('PASS 5/5; DOM-stub interaction checks, no live NAS/browser actions');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
