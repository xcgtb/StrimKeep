// Verify overview missing-episode posters share the existing cached image loader.
const assert = require('assert');
const vm = require('vm');
const {asset} = require('./frontend_source.cjs');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {id, innerHTML:'',textContent:'',hidden:false,style:{},classList:{toggle(){}},scrollLeft:0});
  return nodes.get(id);
};
const hydrateCalls=[];
const esc = x => String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ctx=vm.createContext({console, Date, Math, JSON, Object, Array, Set, Number, String, $, icon:()=>'', esc,
  document:{hidden:false,addEventListener(){},querySelectorAll(){return[];}}, window:{},
  hydratePosters(el){hydrateCalls.push(el.id);},
  localStorage:{getItem(){return null;},setItem(){}},
  api(){throw Error('renderer must not initiate a network request');}
});
function $(id){return node(id);}
vm.runInContext(asset('/static/js/dashboard.js'),ctx);
const data={status:'success', recent:[], missing:[
  {title:'黑道教父',poster:'/api/tmdb/poster/abc.jpg',have:30,total:40,diff:10},
  {title:'无海报',poster:'',have:8,total:9,diff:1},
  {title:'<坏标题>',poster:'javascript:alert(1)',have:0,total:2,diff:2},
], subscriptions:{total:0,enabled:0,pending:0},morning:{enabled:false}};
ctx.renderOverviewActivity(data,{libraryHealth:{stats:{missing:3}}});
const html = node('overviewMissing').innerHTML;
assert(html.includes('class="overview-gap-poster"'));
assert(html.includes('data-emby-src="/api/tmdb/poster/abc.jpg"'));
assert(html.includes('黑道教父') && html.includes('已入库 30 / 40 集') && html.includes('缺 10 集'));
assert(!html.includes('javascript:alert(1)'));
assert(html.includes('&lt;坏标题&gt;') && !html.includes('<坏标题>'));
assert(html.includes('class="overview-empty"'));
assert(html.includes('onclick="overviewOpenHealth(\'missing\')"'));
assert(hydrateCalls.includes('overviewMissing'));
console.log('PASS missing posters reuse cached thumbnails and image hydration; fallback, escaping, navigation preserved');
