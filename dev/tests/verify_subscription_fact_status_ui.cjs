// Exercise real frontend functions with DOM substitutes, not a visual browser.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const elements={},toasts=[];
const box=id=>elements[id]||(elements[id]={textContent:'',innerHTML:'',style:{}});
let response={};
const ctx=vm.createContext({Date,Number,String,Object,Array,Math,Set,currentSubs:[],REC_GROUP_OPEN_MAX:10,
 $:box,esc:x=>String(x==null?'':x).replaceAll('&','&amp;').replaceAll('<','&lt;'),
 api:async()=>response,toast:(msg,kind)=>toasts.push([msg,kind]),hydratePosters(){},
 libraryFactsLabel:s=>'事实时间 '+s.facts_ts+(s.stale?' · TMDB 对照已过期':''),confirm:()=>false});
vm.runInContext(html.slice(html.indexOf('function renderSubscriptionCheckStatus('),html.indexOf('/* ═══════════ 每日晨报')),ctx);
vm.runInContext(html.slice(html.indexOf('function recSubRow('),html.indexOf('function recCard(')),ctx);
(async()=>{
  response={status:'success',enabled:true,subscriptions:[{id:'a',name:'临时剧',have_eps:13,latest_ep:'S01E01',enabled:true,
    facts_ts:200,stale:true,tmdb_declared:0,tmdb_total:20,check_error:'TMDB <down>'}]};
  await ctx.loadSubscriptions();
  assert(box('subList').innerHTML.includes('当前入库: 13 集') && box('subList').innerHTML.includes('已通知至:'));
  assert(box('subList').innerHTML.includes('TMDB 0 集') && box('subList').innerHTML.includes('已过期'));
  assert(box('subList').innerHTML.includes('TMDB &lt;down>'));
  console.log('PASS subscription card separates current facts from notification history, zero totals, stale and failure');
  ctx.renderSubscriptionCheckStatus({status:'partial',failed:1,error:'TMDB down'});
  assert(box('subCheckState').textContent.includes('部分失败') && box('subCheckResult').textContent.includes('1 部失败'));
  console.log('PASS partial checks retain a visible reason and failed count');
  const records=ctx.recSubsHtml({shows:[{name:'failed show',status:'error',error:'Emby down',changed:false}]});
  assert(records.includes('失败/不完整') && !records.includes('无变化') && !records.includes('已入库 0'));
  console.log('PASS failed shows never appear in unchanged records or fabricate zero episodes');
  ctx.loadSubscriptions=async()=>{};
  response={status:'partial',failed:1,checked:1,error:'部分失败',updates:[{name:'临时剧',new_eps:['S01E03']}],
    rows:[{name:'临时剧',status:'partial',error:'TMDB down'}]};
  await ctx.checkSubsNow();
  assert(box('subUpdates').innerHTML.includes('S01E03') && box('subUpdates').innerHTML.includes('TMDB down'));
  assert(box('subCheckState').textContent.includes('部分失败'));
  console.log('PASS partial checks display verified new episodes alongside errors');
  toasts.length=0;response={status:'error',failed:1,checked:0,error:'Emby down',updates:[],rows:[{name:'临时剧',status:'error',error:'Emby down'}]};
  await ctx.checkSubsNow();
  assert(toasts.every(([s])=>!s.includes('暂无')) && box('subCheckState').textContent.includes('失败'));
  console.log('PASS total observation failure does not show a no-change toast');
  toasts.length=0;response={status:'disabled',skipped:'disabled',updates:[],rows:[],checked:0};
  await ctx.checkSubsNow();
  assert(toasts.some(([s])=>s.includes('已停用')) && !toasts.some(([s])=>s.includes('检查失败')));
  console.log('PASS disabled checks are shown as disabled');
  console.log('PASS 6/6; DOM-stub behavior checks only');
})().catch(error=>{console.error(error);process.exit(1);});
