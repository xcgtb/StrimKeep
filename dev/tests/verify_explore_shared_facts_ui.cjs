// Real frontend functions with DOM substitutes; no live browser visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const box={innerHTML:''}; let apiCalls=0,modal='';
const ctx=vm.createContext({Date,Math,String,Number,Array,Set,esc:String,jsarg:JSON.stringify,
 $:()=>box, api:async()=>{apiCalls++;throw Error('no live episode requests');},
 window:{__embySeriesMap:{}},openModal:h=>{modal=h;},hydratePosters(){}});
vm.runInContext(html.slice(html.indexOf('function libraryEpisodeTotal('),html.indexOf('function renderExploreCards(')),ctx);
vm.runInContext(html.slice(html.indexOf('function loadLibBreakdown('),html.indexOf('function renderMovieCard(')),ctx);
vm.runInContext(html.slice(html.indexOf('function showEmbyDetailById('),html.indexOf('async function deleteSeriesFromLib(')),ctx);
const now=Date.now()/1000;
let c={type:'tv',in_emby:true,in_local:true,eps_source:'shared_cache',facts_ts:now,facts_stale:false,
 eps:{have:13,local:13,share:0,total:20,match_status:'missing'}};
assert(ctx.exploreStatus(c).status.includes('13/20') && !ctx.exploreStatus(c).status.includes('连载中'));
assert(ctx.exploreStatus(c).tip.includes('集数更新') && !ctx.exploreStatus(c).tip.includes('查询未成功'));
console.log('PASS exploration labels shared 13/20 facts without claiming a failed live request or ongoing status');
c.eps=null;assert(ctx.exploreStatus(c).status==='集数待同步');
c.eps_source='ambiguous';assert(ctx.exploreStatus(c).status==='身份冲突');
c.eps={have:0,total:20};assert(ctx.exploreStatus(c).status==='未入库');
c.eps={have:13,total:null,match_status:'unmatched'};assert(!ctx.exploreStatus(c).status.includes('完整'));
console.log('PASS missing facts, identity conflict, explicit zero and unknown TMDB total are distinct');
ctx.loadLibBreakdown({seasons:[{season:1,local_eps:1,share_eps:2},{season:2,local_eps:1,share_eps:0}]},
 {seasons:[{season:1,tmdb:2},{season:2,tmdb:1}]});
assert(box.innerHTML.includes('S01') && box.innerHTML.includes('S02') && box.innerHTML.includes('<td>2</td>'));
assert(box.innerHTML.includes('lib-season-count">1</span>') && box.innerHTML.includes('lib-season-count">2</span>'));
assert(box.innerHTML.includes('lib-season-status missing">缺 1 集') && box.innerHTML.includes('lib-season-status complete">✓ 完整'));
assert(box.innerHTML.includes('lib-breakdown-table') && box.innerHTML.includes('lib-breakdown-scroll'));
ctx.loadLibBreakdown({seasons:[{season:1,local_eps:13,share_eps:0}]},{seasons:[{season:1,tmdb:12}]});
assert(box.innerHTML.includes('lib-season-status extra">超 1 集') && box.innerHTML.includes('缺 12 集'));
ctx.loadLibBreakdown({seasons:[{season:1,local_eps:13,share_eps:0}]},{});
assert(box.innerHTML.includes('待对照') && !box.innerHTML.includes('✓ 完整') && !box.innerHTML.includes('缺 '));
assert(apiCalls===0);
console.log('PASS season details use the same cached library counts without live requests');
ctx.loadLibBreakdown({local_eps:13,share_eps:0,seasons:[{season:1,episodes:13}]},{});
assert(box.innerHTML.includes('本地 13 集') && box.innerHTML.includes('尚无逐季分库明细'));
console.log('PASS legacy snapshots explain missing season detail instead of fabricating counts');
ctx.window.__embySeriesMap.s1={id:'s1',name:'测试剧',have_eps:0,total_episodes:20,local_eps:0,
 facts_ts:now,stale:true,tmdb_info:{declared_total:0,tmdb_total:20},seasons:[]};
ctx.showEmbyDetailById('s1');
assert(modal.includes('0 季 · 0 集') && !modal.includes('0/20') && modal.includes('集数更新'));
assert(modal.includes('TMDB 对照已过期') && apiCalls===0);
console.log('PASS detail preserves explicit zero, timestamps and stale comparison state');
ctx.window.__embySeriesMap.s1._md=true;
ctx.showEmbyDetailById('s1');assert(modal.includes('✓ 完整') && modal.includes('取消完结'));
c.eps={have:13,total:20,manual_done:true};assert(ctx.exploreStatus(c).status.startsWith('完整 ·'));
c.eps={have:0,total:20,manual_done:true};assert(ctx.exploreStatus(c).status.startsWith('完整 · 0/20'));
c.eps={have:20,total:20,match_status:'aligned'};assert(ctx.exploreStatus(c).status.startsWith('完整 ·'));
assert(html.includes('>完整 <i class="n" id="mc-aligned"') && !html.includes("statusText = '✓ 对齐'"));
assert(html.includes("if (s._md) { statusText = '✓ 完整'"));
console.log('PASS filters, mapping posters, exploration and manual-completion details use the complete label');
console.log('PASS 6/6; DOM-stub behavior checks only');
