// Execute real scripts in shipped order with a DOM substitute; no visual claims.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const {asset}=require('./frontend_source.cjs');
const html=fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8');
const scripts=Array.from(html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g));
const urls=scripts.map(m=>m[1].match(/src="([^"]+)"/)).filter(Boolean).map(m=>m[1]);
assert.equal(urls.length,14);
for(const url of urls.concat(Array.from(html.matchAll(/<link\b[^>]*href="([^"]+)"/g),m=>m[1]))){
  const content=fs.readFileSync(path.join(__dirname,'../../',new URL(url,'http://fixture').pathname.slice(1))),version=new URL(url,'http://fixture').searchParams.get('v');
  assert.equal(version,crypto.createHash('sha256').update(content).digest('hex').slice(0,12));
}
assert(scripts.filter(m=>/src=/.test(m[1])).every(m=>/\bdefer\b/.test(m[1])&&!/\basync\b|type="module"/.test(m[1])));
assert(!/<style\b/.test(html));
console.log('PASS all shipped assets resolve with content versions and ordered deferred loading');
const events={},windowEvents={},timers=[],nodes={};
const list=()=>({add(){},remove(){},toggle(){},contains(){return false;}});
function node(){return {style:{setProperty(){}},classList:list(),dataset:{},textContent:'',innerHTML:'',children:[],childNodes:[],
  getAttribute(){return null;},setAttribute(){},querySelector(){return null;},querySelectorAll(){return [];},insertAdjacentHTML(){},appendChild(){},addEventListener(){}};}
const document={readyState:'loading',documentElement:node(),body:node(),querySelector(){return null;},querySelectorAll(){return [];},
  getElementById:id=>nodes[id]||(nodes[id]=node()),createElement:()=>node(),
  addEventListener:(name,fn)=>(events[name]||(events[name]=[])).push(fn)};
const storage={getItem(){return null;},setItem(){},removeItem(){}};
const context=vm.createContext({console,document,localStorage:storage,sessionStorage:storage,URL,URLSearchParams,
  AbortController,Promise,Date,Math,JSON,Number,String,Object,Array,Set,Map,encodeURIComponent,decodeURIComponent,
  MutationObserver:class {observe(){}},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},setInterval:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearInterval(){},
  requestAnimationFrame:fn=>{timers.push({fn,ms:16});return timers.length;},
  fetch(){throw Error('unexpected pre-initialization network request');}});
context.window=context;context.innerWidth=390;context.innerHeight=800;context.scrollY=0;
context.matchMedia=()=>({matches:false,addEventListener(){}});
context.addEventListener=(name,fn)=>(windowEvents[name]||(windowEvents[name]=[])).push(fn);
for(const [i,m] of scripts.entries()){
  const src=m[1].match(/src="([^"]+)"/),code=src?asset(src[1]):m[2];
  vm.runInContext(code,context,{filename:src?src[1]:'inline-'+i});
}
assert.equal(events.DOMContentLoaded.length,1); // Main initialization; CSS owns dashboard layout.
for(const name of ['scanLibrary','onTaskHintClose','loadExplore','exploreMore','loadEmbyLibrary','embyMore','scanEmptyDirs','cleanEmptyDirs','toggleSubscribe','loadRecords','saveConfig','switchTab']){
  assert.equal(typeof context[name],'function',name+' must stay reachable by HTML events');
}
assert.equal(Object.keys(context.__taskHintControl||{}).length,0);
console.log('PASS scripts evaluate in delivery order, retain global handlers and register initialization once');
(async()=>{
  const calls=[];
  for(const name of ['initTheme','injectIcons','renderMenu','switchExploreTab_init','buildSubnavs','hydrateDashboardFromCache','loadDashboard','setupPosterAutoLoad','loadLibraryStats','pollTmdbProgress','startRuntimeStatus'])
    context[name]=()=>calls.push(name);
  let resolveSubscriptions;
  context.api=()=>new Promise(resolve=>{resolveSubscriptions=resolve;});
  await events.DOMContentLoaded[0]();
  assert.deepEqual(calls,['initTheme','injectIcons','renderMenu','switchExploreTab_init','buildSubnavs','hydrateDashboardFromCache','loadDashboard','setupPosterAutoLoad','loadLibraryStats','pollTmdbProgress','startRuntimeStatus']);
  assert.equal(context.window.__activeTab,'dashboard');
  resolveSubscriptions({status:'success',subscriptions:[]});
  await context.exploreSubscriptionsPromise;
  console.log('PASS startup loads the dashboard and poster handlers without waiting for the subscription request');
  console.log('PASS 3/3; full shipped script order with DOM substitutes only');
})().catch(e=>{console.error(e);process.exitCode=1;});
