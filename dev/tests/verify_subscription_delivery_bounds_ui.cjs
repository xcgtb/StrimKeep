// Shipped subscription UI; DOM/network substitutes, not a live browser.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../../static/js/subscriptions.js'),'utf8');
const elements={};
const box=id=>elements[id]||(elements[id]={textContent:'',innerHTML:'',style:{},value:'',checked:false});
let request;
const ctx=vm.createContext({Date,Number,String,Object,Array,Math,Set,JSON,parseInt,currentSubs:[],
  $:box,esc:String,hydratePosters(){},toast(){},confirm:()=>false,
  api:async(url,options)=>{request={url,options};return {status:'success'};}});
vm.runInContext(source,ctx);
(async()=>{
  ctx.renderSubscriptionTriggerHint({ingest_enabled:true,notification:{pending:0}});
  assert(box('subTriggerHint').style.display==='none');
  ctx.renderSubscriptionTriggerHint({ingest_enabled:false,notification:{pending:2,last_error:'Telegram <offline>',reconcile_required:true}});
  assert(box('subTriggerHint').textContent.includes('入库监控已关闭'));
  assert(box('subTriggerHint').textContent.includes('待发送 2 批'));
  assert(box('subTriggerHint').textContent.includes('自动校准补报'));
  assert(box('subTriggerHint').innerHTML==='');
  console.log('PASS disabled ingest, pending retry and backpressure are visible as safe text');
  ctx.renderSubscriptionCheckStatus({status:'success',checked:1,notification:{pending:0,reconcile_required:false},ingest_enabled:true});
  assert(box('subTriggerHint').style.display==='none');
  console.log('PASS delivery status refresh clears recovered backlog hints');
  ctx.renderSubscriptionTriggerHint({ingest_enabled:true,notification:{status:'unavailable',error:'SQLite <failed>'}});
  assert(box('subTriggerHint').textContent.includes('状态不可用') && box('subTriggerHint').textContent.includes('<failed>'));
  assert(box('subTriggerHint').innerHTML==='');
  console.log('PASS failed status lookup remains unknown, never a false zero backlog');
  box('subEnabled').checked=true;box('subCheckTmdb').checked=true;box('subInterval').value='6';
  await ctx.saveSubSettings();
  const body=JSON.parse(request.options.body);
  assert(request.url==='/api/subscriptions/settings' && body.calibration_interval_hours===6 && !('interval_min' in body));
  console.log('PASS settings save hours instead of accidentally retaining minute polling');
  console.log('PASS 4/4; DOM substitutes, no real Telegram or browser claims');
})().catch(error=>{console.error(error);process.exit(1);});
