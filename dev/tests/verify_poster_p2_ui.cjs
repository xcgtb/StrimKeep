// Deterministic tests against exact shipped P2 frontend functions.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const repo=path.join(__dirname,'../..');
const exp=fs.readFileSync(path.join(repo,'static/js/explore.js'),'utf8');
const map=fs.readFileSync(path.join(repo,'static/js/mapping.js'),'utf8');
const readSlice=(s,a,b)=>{assert(s.includes(a)&&s.includes(b));return s.slice(s.indexOf(a),s.indexOf(b));};
const store={};let clock=10000000;
const sessionStorage={getItem:k=>store[k],setItem:(k,v)=>store[k]=v};
const explore=vm.createContext({sessionStorage,Date:{now:()=>clock},JSON,Array});
vm.runInContext(readSlice(exp,'var EXPLORE_SESSION_PREVIEW_KEY','function ensureExplorePrefetch(){'),explore);
const page={status:'success',total_pages:9,cards:[{type:'tv',tmdb_id:'42',poster:'/api/tmdb/poster/example.jpg',title:'Example',year:'2026',rating:7.9,
  in_emby:true,in_local:true,eps:{have:12,total:12},facts_ts:99}]};
explore.saveExploreSessionPreview('region=all&page=1',page);
const cache=explore.readExploreSessionPreview('region=all&page=1');
assert.equal(cache.cards.length,1);assert.equal(cache.cards[0].library_status,'stale');
assert.equal(cache.cards[0].facts_stale,true);
assert.equal(cache.cards[0].eps,undefined);assert.equal(cache.cards[0].in_emby,undefined);
assert.equal(explore.readExploreSessionPreview('q=another'),null);
clock+=1800001;assert.equal(explore.readExploreSessionPreview('region=all&page=1'),null);
console.log('PASS session preview is bounded, query-scoped, expires, and contains no stale membership/episode facts');
const failed=vm.createContext({sessionStorage:{getItem(){throw Error('disabled');},setItem(){throw Error('disabled');}},Date:{now:()=>clock},JSON,Array});
vm.runInContext(readSlice(exp,'var EXPLORE_SESSION_PREVIEW_KEY','function ensureExplorePrefetch(){'),failed);
assert.equal(failed.readExploreSessionPreview('x'),null);
assert.doesNotThrow(()=>failed.saveExploreSessionPreview('x',page));
assert(exp.includes('if (preview && generation === exploreGeneration)') &&
  exp.includes('if (!append && requestedPage === 1) saveExploreSessionPreview(qs, r)'));
console.log('PASS unavailable sessionStorage is nonfatal and preview reconciles through the normal page request');
const entries=[{name:'z',tmdb_info:{match_status:'missing',diff:-4}},
  {name:'b',tmdb_info:{match_status:'aligned',diff:0}},
  {name:'a',tmdb_info:{match_status:'missing',diff:-2}}];
const ctx=vm.createContext({embyData:{series:entries},Math});
vm.runInContext(readSlice(map,'var __mappingSortedCache =','function renderEmbyList(){'),ctx);
const first=ctx.sortedMappingSeries(),second=ctx.sortedMappingSeries();
assert.strictEqual(first,second);assert.deepEqual(Array.from(first,x=>x.name),['z','a','b']);
assert.deepEqual(Array.from(entries,x=>x.name),['z','b','a']); // Source order not mutated.
ctx.embyData.series=[...entries,{name:'q',tmdb_info:{match_status:'extra',diff:2}}];
assert.notStrictEqual(ctx.sortedMappingSeries(),first);
assert(map.includes('__mappingSortedCache = {source:null, rows:null};'));
console.log('PASS mapping sort memoizes same revision, reorders on replacement and preserves original data');
console.log('PASS P2 frontend 3/3');
