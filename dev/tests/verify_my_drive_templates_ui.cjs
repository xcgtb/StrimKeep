// Exercise real template/classification UI requests, without browser claims.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[];
const categoryNodes=['电影','剧集','动漫','纪录片','综艺','儿童节目','演唱会'].map(name=>({dataset:{category:name},checked:true}));
function node(id){return nodes[id]||(nodes[id]={dataset:{},value:'',textContent:'',innerHTML:'',hidden:false,checked:false,children:[],setAttribute(){},classList:{toggle(){}},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},addEventListener(event,fn){events[id+':'+event]=fn;},querySelectorAll(selector){if(id==='cw-categories')return selector.endsWith(':checked')?categoryNodes.filter(n=>n.checked):categoryNodes;return [];}});}
const config={naming_style:'template',movie_folder:'电影',tv_folder:'电视剧',rename_enabled:true,classification_enabled:true,enabled_categories:['电影','剧集','动漫','纪录片','综艺'],custom_categories:['短剧'],region_mode:'grouped',region_groups:[{name:'国产',countries:['CN','HK','TW']},{name:'韩国',countries:['KR']}],animation_movie_folder:true,ffprobe_enabled:true,ffprobe_failure:'skip',region_classification:true,year_classification:true,unknown_enabled:true,movie_directory_template:'{title} ({year}) {{tmdb-{tmdb_id}}}',tv_directory_template:'{title} ({year}) [tmdb-{tmdb_id}]',season_directory_template:'Season{season:02d}',movie_file_template:'{title}.{year}{ext}',tv_file_template:'{title}.{year}.S{season:02d}E{episode:02d}{ext}'};
const context={console,URLSearchParams,Promise,Object,Array,Number,String,JSON,esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),document:{getElementById:node,createElement:()=>node('created-'+Math.random())},setTimeout(){},clearTimeout(){},showModalConfirm:async()=>true,fetch(url,opt){if(url==='/api/cloud/status')return Promise.resolve({ok:true,json:async()=>({accounts:[{id:'A',uid:'123',name:'测试',authorization:{cookie:true}}]})});if(url.endsWith('/directories'))return Promise.resolve({ok:true,json:async()=>({directories:{source:{id:'1',name:'来源'},target:{id:'2',name:'归档'},transfer:null,unknown:{id:'3',name:'未识别'}},revision:1})});if(url==='/api/cloud/settings'&&!opt.method)return Promise.resolve({ok:true,json:async()=>({settings:config,capabilities:{ffprobe_available:true}})});if(url==='/api/cloud/rules')return Promise.resolve({ok:true,json:async()=>({items:[]})});return new Promise(resolve=>requests.push({url,opt,resolve}));}};
context.window=context;context.__activeTab='cloud';vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/cloud-workbench.js'),'utf8'),context);
const respond=(r,body,status=200)=>r.resolve({ok:status<400,status,json:async()=>body});
async function flush(){for(let i=0;i<30;i++)await Promise.resolve();}
function click(action){events['tab-cloud:click']({target:{closest:s=>s==='[data-cwa]'?{dataset:{cwa:action}}:null}});}
function tab(value){events['tab-cloud:click']({target:{closest:s=>s==='[data-cw-tab]'?{dataset:{cwTab:value}}:null}});}
async function run(){
 await context.cloudWorkbench.load();tab('organize');await flush();
 assert.strictEqual(node('cw-custom-categories').value,'短剧');
 assert(!node('cw-template-fields').hidden);
 assert(node('cw-ffprobe-enabled').checked);assert(node('cw-animation-movie-folder').checked);assert.strictEqual(node('cw-region-mode').value,'grouped');assert(node('cw-ffprobe-status').textContent.includes('已就绪'));assert(node('cw-region-groups').innerHTML.includes('韩国'));
 node('cw-region-groups').onchange({target:{dataset:{regionIndex:'1',regionKey:'countries'},value:'KR,JP'}});
 click('fullMediaTemplate');assert(node('cw-tv_file_template').value.includes('{fps}.{bitdepth}'));
 const originalDraft=node('cw-tv_file_template').value;
 click('templateDetails');await flush();let detail=requests.shift();assert.strictEqual(detail.url,'/api/cloud/naming/details');
 respond(detail,{template:{directory:'{title}',tv_directory:'{title}',season_directory:'Season 01',movie_file:'{title}{ext}',tv_file:originalDraft,items:[{kind:'tv',directory:'剧集/演示',name:'<episode>.mkv'}],notes:['只读示例']}});await flush();
 assert(node('cw-template-details').innerHTML.includes('&lt;episode>')&&node('cw-tv_file_template').value===originalDraft);
 assert(requests.length===0,'viewing details cannot submit settings or mutate files');

 node('cw-custom-categories').value='短剧\n演示分类';node('cw-custom-categories').oninput();
 assert(node('cw-category-choice').children.some(n=>n.value==='演示分类'));
 click('previewNaming');await flush();let r=requests.shift();assert.strictEqual(r.url,'/api/cloud/naming/preview');let body=JSON.parse(r.opt.body);assert.deepStrictEqual(body.settings.custom_categories,['短剧','演示分类']);assert(body.settings.ffprobe_enabled);assert.strictEqual(body.settings.region_groups[1].countries.join(','),'KR,JP');assert.strictEqual(body.settings.region_mode,'grouped');assert.strictEqual(body.settings.season_directory_template,'Season{season:02d}');respond(r,{items:[{kind:'movie',directory:'电影/示例',name:'示例.mkv'}]});await flush();assert(node('cw-naming-preview').children[0].textContent.includes('格式示例'));
 click('plan');await flush();assert.strictEqual(requests.length,0);assert(node('cw-message').textContent.includes('先保存'));
 click('saveNaming');await flush();r=requests.shift();assert.strictEqual(r.url,'/api/cloud/settings');assert(JSON.parse(r.opt.body).unknown_enabled);respond(r,{});await flush();
 click('plan');await flush();r=requests.shift();assert.strictEqual(r.url,'/api/cloud/plans');assert.strictEqual(JSON.parse(r.opt.body).unknown_cid,'3');respond(r,{plan:{id:'plan',items:[],unknown_items:[{source:{id:'4'}}],skipped:[{file_id:'4',name:'Unknown.mkv',reason:'未匹配',action:'unknown',target:'来源-1/Unknown.mkv'}]}});await flush();assert(node('cw-plan-list').innerHTML.includes('未识别目录'));assert(node('cw-message').textContent.includes('移入未识别 1'));
 click('apply');await flush();r=requests.shift();assert.strictEqual(r.url,'/api/cloud/plans/apply');assert.strictEqual(JSON.parse(r.opt.body).plan_id,'plan');respond(r,{job_id:'job'});await flush();
 // Jobs read and details run concurrently after submit.
 for(let wave=0;wave<3;wave++){requests.splice(0).forEach(r=>respond(r,r.url.endsWith('/job')?{job:{state:'success',message:'完成',steps:[],result:{unknown:[{name:'Unknown.mkv'}]}}}:{items:[]}));await flush();}assert(node('cw-job-summary').children.some(n=>n.textContent.includes('已移入未识别')));
 tab('organize');await flush();click('createCategories');await flush();r=requests.shift();assert.strictEqual(r.url,'/api/cloud/settings');respond(r,{});await flush();r=requests.shift();assert.strictEqual(r.url,'/api/cloud/categories/create');assert.strictEqual(JSON.parse(r.opt.body).target_cid,'2');assert.strictEqual(JSON.parse(r.opt.body).confirmed,true);
 console.log('PASS my drive template preview, multiline custom categories, unsaved plan guard, unknown plan and category creation requests');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
