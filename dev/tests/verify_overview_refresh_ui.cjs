// Execute shipped dashboard logic; controlled network, clock and DOM.
const assert=require('assert'),vm=require('vm');
const {asset}=require('./frontend_source.cjs');
const nodes=new Map(),saved=new Map(),pending=[],timers=new Map(),toasts=[];
let timerId=0,now=100000;
function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',disabled:false,scrollLeft:0,clientWidth:700,
  classList:{toggle(){}},scrollBy(){},style:{},querySelectorAll(){return [];}});return nodes.get(id);}
const ctx=vm.createContext({console,Number,Object,Array,JSON,Math,Promise,Set,Map,encodeURIComponent,
  Date:class extends Date {static now(){return now;}},$:node,esc:v=>String(v).replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  document:{hidden:false,addEventListener(){},querySelectorAll(){return [];}},
  localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
  setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id),
  icon:()=>'',fmtGovAgo:()=>'',renderSidebarServices(){},hydratePosters(){},toast:(...x)=>toasts.push(x),
  api:(url,opts)=>new Promise((resolve,reject)=>pending.push({url,opts,resolve,reject}))});
ctx.window=ctx;ctx.__activeTab='dashboard';
vm.runInContext(asset('/static/js/dashboard.js'),ctx);
ctx.loadStorageStatus=()=>{};
function req(prefix){const i=pending.findIndex(x=>x.url.startsWith(prefix));assert(i>=0,'missing '+prefix);return pending.splice(i,1)[0];}
async function flush(){for(let i=0;i<16;i++)await Promise.resolve();}
function snapshot(job={running:false,id:0}){return {status:'success',ts:now/1000,dashboard:{localCount:'18',shareCount:'20',lastScan:null},library_stats:{rows:[],local_total:18,share_total:20},strategy:{decision:'quality_first'},refresh:job};}
(async()=>{
  saved.set('overviewCache',JSON.stringify(snapshot()));ctx.hydrateDashboardFromCache();assert.equal(node('stat-local').textContent,'18');assert.equal(pending.length,0);
  ctx.loadDashboard();ctx.loadDashboard();assert.equal(pending.filter(x=>x.url==='/api/overview').length,1);
  req('/api/overview').resolve(snapshot());await flush();
  assert.equal(node('stat-share').textContent,'20');assert(pending.some(x=>x.url.includes('recommendations')),'recommendations remain pending while counts are ready');
  console.log('PASS cached hydration is immediate; repeated opens reuse a request and slow recommendations do not block statistics');
  req('/api/overview/recommendations').resolve({status:'success',page_ts:100,cards:[{title:'<img src=x>',tmdb_id:'1',type:'movie',year:'2026',rating:8,poster:''}]});await flush();
  assert(node('overviewHotShelf').innerHTML.includes('&lt;img'));assert(!node('overviewHotShelf').innerHTML.includes('已在库'));assert(!node('overviewHotShelf').innerHTML.includes('未入库'));
  ctx.loadDashboard();await flush();assert.equal(pending.length,0);
  console.log('PASS recommendations escape names and distinguish unknown library state; quick return does not refetch');
  ctx.refreshOverview();ctx.refreshOverview();assert.equal(pending.filter(x=>x.url==='/api/overview/refresh').length,1);
  req('/api/overview/refresh').resolve({status:'success',refresh:{id:1,running:true,phase:'正在更新片库数据'}});await flush();
  assert(!saved.has('overviewCache'));assert.equal(node('stat-local').textContent,'18');
  req('/api/overview').resolve(snapshot({id:1,running:true,phase:'正在更新片库数据'}));await flush();
  assert(node('overviewRefresh').disabled);assert(node('overviewRefreshState').textContent.includes('完成后自动更新'));
  const timer=timers.get(ctx.overviewControl.timer);assert.equal(timer.ms,1500);timer.fn();
  const done=snapshot({id:1,running:false});done.dashboard.localCount='25';req('/api/overview').resolve(done);await flush();
  assert.equal(node('stat-local').textContent,'25');assert(!node('overviewRefresh').disabled);assert.equal(toasts.length,1);assert(saved.has('overviewCache'));
  console.log('PASS single manual refresh clears browser cache, preserves current content and automatically displays completion');
  now+=20000;ctx.loadDashboard(true);req('/api/overview').reject(Error('offline'));await flush();assert.equal(node('stat-local').textContent,'25');assert(node('overviewRefreshState').textContent.includes('保留缓存'));
  ctx.pauseOverview();assert(!timers.has(ctx.overviewControl.timer));assert(!timers.has(ctx.overviewHot.timer));
  console.log('PASS failures preserve previous values and navigation pauses polling');
  console.log('PASS 4/4; network/DOM substitutes, browser visuals validated separately');
})().catch(e=>{console.error(e);process.exitCode=1;});
