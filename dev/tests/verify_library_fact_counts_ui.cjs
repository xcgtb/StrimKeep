// Real dashboard functions with a DOM substitute; no browser visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const nodes=new Map();let response,fail=false;const saved=new Map();
const node=id=>{
 if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',style:{setProperty(){}},value:''});return nodes.get(id);
};
const ctx=vm.createContext({$:node,Date,Math,JSON,esc:String,
 api:async()=>{if(fail)throw new Error('offline');return response;},
 embyLoaded:false,embyData:{series:[],movies:[],stats:{}},md_sync:(s,stats)=>stats,
 localStorage:{setItem:(k,v)=>saved.set(k,v)},switchTab(){},renderEmbyList(){}});
vm.runInContext(html.slice(html.indexOf('function fmtBig('),html.indexOf('function renderDashPlanFromDashboard(')),ctx);
async function main(){
 const now=Date.now()/1000;
 response={status:'success',ts:now-4000,facts_ts:now,facts_version:'v13',episodes:13,
  series:[{have_eps:13}],movies:[],stats:{total_series:1,missing:1},top_missing:[],stale:true};
 await ctx.loadDashEmby();assert(node('dash-eps').innerHTML==='13');
 assert(node('dash-library-facts').textContent.includes('集数更新') && node('dash-library-facts').textContent.includes('已过期'));
 assert(JSON.parse(saved.get('dashEmbyCache')).facts_version==='v13');
 console.log('PASS dashboard uses shared 13-episode total and preserves fact version/time in local cache');
 ctx.renderDashEmby(JSON.parse(saved.get('dashEmbyCache')));
 assert(node('dash-library-facts').textContent.includes('已过期'));
 console.log('PASS cached hydration labels comparison age instead of claiming a fresh scan');
 fail=true;ctx.embyLoaded=true;ctx.embyData={series:[{have_eps:0,total_episodes:20}],movies:[],stats:{}};
 await ctx.loadDashEmby();assert(node('dash-eps').innerHTML==='0');
 console.log('PASS fallback retains explicit zero instead of restoring obsolete 20-episode count');
 console.log('PASS 3/3; DOM-stub behavior checks only');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
