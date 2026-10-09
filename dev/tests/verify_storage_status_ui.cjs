// Actual rendering functions; DOM substitutes, not live visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const panel={style:{}},list={innerHTML:''};
const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
let requests=0;
const ctx=vm.createContext({Date,Object,esc,$:id=>id==='dash-storage-warning'?panel:list,
 api:async url=>{requests++;assert.equal(url,'/api/storage/status');return {status:'success',storage:{issues:[]}};}});
vm.runInContext(html.slice(html.indexOf('function renderStorageStatus(status){'),html.indexOf('function renderDashboard(d){')),ctx);
ctx.renderStorageStatus({status:'no_observed_error',issues:[]});assert.equal(panel.style.display,'none');
console.log('PASS normal observations keep the warning panel hidden');
ctx.renderStorageStatus({status:'degraded',issues:[{operation:'db_add_audit',message:'存储空间不足',last_at:100}]});
assert.equal(panel.style.display,'');assert(list.innerHTML.includes('执行记录保存')&&list.innerHTML.includes('存储空间不足'));
console.log('PASS failed persistence shows a readable operation, classified reason and last occurrence');
ctx.renderStorageStatus({issues:[{operation:'unknown',message:'<img src=x onerror=alert(1)>',last_at:1}]});
assert(!list.innerHTML.includes('<img'));assert(list.innerHTML.includes('&lt;img'));
console.log('PASS warning content is escaped rather than inserted as markup');
let cached='';ctx.localStorage={setItem:(key,value)=>cached=value};
vm.runInContext(html.slice(html.indexOf('function saveDashboardCache(d){'),html.indexOf('function saveLibStatsCache(r){')),ctx);
ctx.saveDashboardCache({localCount:'1',storage:{issues:['old']}});assert(!JSON.parse(cached).storage);
assert(html.includes('if (d.storage) renderStorageStatus(d.storage);'));
console.log('PASS dashboard local cache cannot replay stale storage errors as fresh observations');
(async()=>{await ctx.loadStorageStatus();assert.equal(requests,1);assert.equal(panel.style.display,'none');
 ctx.api=async()=>{throw Error('unavailable');};panel.style.display='';await ctx.loadStorageStatus();assert.equal(panel.style.display,'');
 console.log('PASS successful retry removes warnings while failed status requests preserve the existing warning');
 console.log('PASS 5/5; DOM-substitute behavior checks only');
})().catch(error=>{console.error(error);process.exitCode=1;});
