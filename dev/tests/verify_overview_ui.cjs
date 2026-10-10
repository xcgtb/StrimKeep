// Scope totals, cache safety and season comparisons with the shipped renderers.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const {asset}=require('./frontend_source.cjs');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',style:{setProperty(){}},classList:{toggle(){}},value:''});return nodes.get(id);};
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let cached={},requests=0;
const ctx=vm.createContext({console,Date,Math,JSON,Object,Array,Set,Number,String,$:node,esc,
 document:{addEventListener(){},querySelectorAll(){return [];}},window:{},
 localStorage:{getItem:k=>cached[k]||null,setItem:(k,v)=>cached[k]=v},
 fmtGovAgo:()=> '刚刚',api(){requests++;throw Error('renderer must not request');}});
vm.runInContext(asset('/static/js/dashboard.js'),ctx);
const stats={local_total:11,share_total:19,local_other:1,share_other:2,rows:[{name:'儿童节目',local:10,share:17,total:27}]};
ctx.libStatMode='local';ctx.renderLibraryStats(stats);
assert.match(node('libStatsSummary').textContent,/本地库 11 个 STRM · 未分类 1/);assert(!node('libStatsSummary').textContent.includes('19'));
ctx.libStatMode='share';ctx.renderLibraryStats(stats);assert.match(node('libStatsSummary').textContent,/分享库 19 个 STRM · 未分类 2/);
ctx.libStatMode='all';ctx.renderLibraryStats(stats);assert.match(node('libStatsSummary').textContent,/两库合计 30 个 STRM/);
console.log('PASS all/local/share summaries use their own totals and unclassified counts');
const hostile='剧名"><img src=x onerror=alert(1)>';
ctx.renderDashEmby({series:1,movies:0,eps:0,st:{missing:1},top:[{name:hostile,year:2000,diff:10,have:0,tot:10}],ts:Date.now()/1000-4000});
assert(!node('dash-health').innerHTML.includes(hostile));
assert(node('dash-facts-summary').textContent.includes('已过期'));assert(node('dash-eps').innerHTML==='0');
assert(node('dash-health-bar').innerHTML.includes('flex-grow:1'));
console.log('PASS overview has only summary completeness, explicit zero counts and stale-cache labels');
ctx.renderDashPlanFromDashboard({});assert(node('overviewGovRecent').innerHTML.includes('暂无治理扫描'));
ctx.icon=()=>'';
ctx.renderDashPlanFromDashboard({lastScan:{local:0,share:2,local_files:0,share_files:8,reason_counts:{local_better:2}}});
assert(node('overviewGovRecent').innerHTML.includes('本地待处理 0 项')&&node('overviewGovRecent').innerHTML.includes('分享待处理 2 项'));
assert(node('dash-gov-reasons').innerHTML.includes('本地画质更优'));
console.log('PASS unscanned governance differs from confirmed zero and retains decision counts');
const mapping=asset('/static/js/mapping.js');vm.runInContext(mapping.slice(mapping.indexOf('function loadLibBreakdown('),mapping.indexOf('function renderMovieCard(')),ctx);
ctx.loadLibBreakdown({seasons:[{season:2,local_eps:8,share_eps:10},{season:0,local_eps:0,share_eps:0}]},{seasons:[{season:0,tmdb:0},{season:1,tmdb:3},{season:2,tmdb:8}]});
const detail=node('libBreakdown').innerHTML;
assert(detail.indexOf('特别篇')<detail.indexOf('S01')&&detail.indexOf('S01')<detail.indexOf('S02'));
assert(detail.includes('缺 3 集')&&detail.includes('超 2 集')&&detail.includes('✓ 完整'));
assert(!detail.includes('<table')&&!detail.includes('overflow:auto'));assert(requests===0);
console.log('PASS sorted season union includes TMDB-only seasons, zero specials and both-library differences without live calls');
console.log('PASS 4/4; DOM substitutes, browser verification recorded separately');
