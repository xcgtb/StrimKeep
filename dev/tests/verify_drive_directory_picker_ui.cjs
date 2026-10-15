// Exercise the shipped workbench: navigation, remote folder browsing and account-scoped saves.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[];
const roles=['source','target','transfer','unknown'];
const tabs=['accounts','files','transfer','organize','strm','playback','jobs'].map(key=>({dataset:{cwTab:key},attrs:{},setAttribute(k,v){this.attrs[k]=v;},classList:{toggle(){}}}));
function node(id){return nodes[id]||(nodes[id]={dataset:{},value:'',textContent:'',innerHTML:'',hidden:false,checked:false,disabled:false,open:false,children:[],attrs:{},setAttribute(k,v){this.attrs[k]=v;if(k==='open')this.open=true;},removeAttribute(k){delete this.attrs[k];if(k==='open')this.open=false;},showModal(){this.open=true;},close(){this.open=false;},classList:{toggle(){}},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},addEventListener(k,fn){events[id+':'+k]=fn;},querySelector(selector){return node(id+selector);},querySelectorAll(){return id==='cw-tabs'?tabs:[];}});}
let configRevision=1;
function directories(account){return {directories:{source:{id:account==='A'?'1':'B1',name:account+'来源'},target:{id:'2',name:'归档'},transfer:null,unknown:null},revision:configRevision};}
const context={console,URLSearchParams,Promise,Object,Array,Number,String,JSON,esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),document:{getElementById:node,createElement:()=>node('new-'+Math.random())},setTimeout(){},clearTimeout(){},fetch(url,opt){
 if(url==='/api/cloud/status')return Promise.resolve({ok:true,json:async()=>({accounts:['A','B'].map(id=>({id,uid:id,name:id+'账号',authorization:{cookie:true}}))})});
 if(url.endsWith('/directories')&&!opt.method)return Promise.resolve({ok:true,json:async()=>directories(url.includes('/A/')?'A':'B')});
 if(url==='/api/cloud/settings')return Promise.resolve({ok:true,json:async()=>({settings:{}})});
 if(url==='/api/cloud/rules')return Promise.resolve({ok:true,json:async()=>({items:[]})});
 return new Promise(resolve=>requests.push({url,opt,resolve}));
}};
context.window=context;vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/cloud-workbench.js'),'utf8'),context);
async function flush(){for(let i=0;i<35;i++)await Promise.resolve();}
function action(key){events['tab-cloud:click']({target:{closest:s=>s==='[data-cwa]'?{dataset:{cwa:key}}:null}});}
function pick(role){events['tab-cloud:click']({target:{closest:s=>s==='[data-directory-role]'?{dataset:{directoryRole:role}}:null}});}
function tab(key){events['tab-cloud:click']({target:{closest:s=>s==='[data-cw-tab]'?{dataset:{cwTab:key}}:null}});}
function enter(index){node('cw-directory-list').onclick({target:{closest:()=>({dataset:{directoryIndex:String(index)}})}});}
function respond(r,body,status=200){assert(r);r.resolve({ok:status<400,status,json:async()=>body});}
function page(items,has_more=false){return {items,total:items.length,has_more};}
function take(){assert(requests.length,'expected request');return requests.shift();}
function button(action){return node('cw-directory-dialog').querySelector('[data-cwa="directory'+action+'"]');}
async function run(){
 await context.cloudWorkbench.load();tab('organize');await flush();
 const html=node('tab-cloud').innerHTML;
 assert(!html.includes('cw-page-picker'));assert(!html.includes('在文件管理中选择目录'));
 for(const role of roles){assert(html.includes('data-directory-role="'+role+'"'));assert.strictEqual((html.match(new RegExp('id="cw-'+role+'-cid"','g'))||[]).length,1);}
 assert.strictEqual(tabs.find(t=>t.dataset.cwTab==='organize').attrs['aria-current'],'page');assert(node('cw-files').hidden);assert(!node('cw-organize').hidden);
 const css=fs.readFileSync(path.join(__dirname,'../../static/css/cloud-workbench.css'),'utf8');assert(css.includes('flex-direction:row;overflow-x:auto'));assert(css.includes('white-space:nowrap'));assert(!css.includes('.cw-mobile-nav'));
 // Hidden video rows do not break pagination. IDs larger than JS safe integers stay strings.
 pick('source');await flush();assert(node('cw-directory-dialog').open);let r=take();assert(r.url.includes('account_id=A&cid=0&offset=0'));assert(button('Confirm').disabled);action('directoryConfirm');assert(!requests.length);
 respond(r,page([{id:'video',name:'Movie.mkv',is_dir:false}],true));await flush();assert(node('cw-directory-list').innerHTML.includes('当前页没有子目录'));assert(!button('Next').disabled);
 action('directoryNext');await flush();r=take();assert(r.url.endsWith('offset=100'));respond(r,page([{id:'3500402610102339321',name:'<目录>&"',is_dir:true}]));await flush();assert(node('cw-directory-list').innerHTML.includes('&lt;目录>'));assert(!node('cw-directory-list').innerHTML.includes('<目录>'));
 action('directoryPrev');await flush();r=take();assert(r.url.endsWith('offset=0'));respond(r,page([{id:'3500402610102339321',name:'待整理',is_dir:true}]));await flush();enter(0);await flush();r=take();assert(r.url.includes('cid=3500402610102339321'));respond(r,page([]));await flush();
 action('directoryConfirm');await flush();assert(!requests.length,'selection must not persist');assert(!node('cw-directory-dialog').open);assert(node('cw-message').textContent.includes('待保存')||node('cw-message').textContent.includes('保存修改'));
 action('saveDirectories');await flush();r=take();assert.strictEqual(r.url,'/api/cloud/accounts/A/directories');assert.deepStrictEqual(JSON.parse(r.opt.body),{directories:{source:'3500402610102339321'},expected_revision:1});action('saveDirectories');assert(!requests.length,'save guarded against duplicate');
 respond(r,{directories:{...directories('A').directories,source:{id:'3500402610102339321',name:'待整理'}},revision:2});await flush();configRevision=2;assert(node('cw-source-cid').value.includes('3500402610102339321'));assert(!node('cw-organize').hidden);
 // Cancel invalidates an outstanding page response, including when another picker is opened.
 pick('target');await flush();let stale=take();action('directoryCancel');pick('unknown');await flush();r=take();respond(r,page([{id:'9',name:'未识别',is_dir:true}]));await flush();respond(stale,page([{id:'8',name:'不应出现',is_dir:true}]));await flush();assert(!node('cw-directory-list').innerHTML.includes('不应出现'));assert.strictEqual(node('cw-directory-title').textContent,'选择未识别目录');
 enter(0);await flush();r=take();respond(r,page([]));await flush();action('directoryParent');await flush();r=take();assert(r.url.includes('cid=0'));respond(r,page([]));await flush();
 // Listing failures block save until a successful retry, and Escape cancels without writes.
 action('directoryRetry');await flush();r=take();respond(r,{detail:'网络暂不可用'},502);await flush();assert(button('Confirm').disabled);assert(node('cw-directory-status').textContent.includes('网络暂不可用'));action('directoryConfirm');assert(!requests.length);action('directoryRetry');await flush();r=take();respond(r,page([]));await flush();assert(!button('Confirm').disabled);let prevented=false;events['cw-directory-dialog:cancel']({preventDefault(){prevented=true;}});assert(prevented);assert(!node('cw-directory-dialog').open);assert.strictEqual(node('cw-unknown-cid').value,'');
 // Conflict preserves the draft and never silently writes newer config.
 pick('transfer');await flush();r=take();respond(r,page([]));await flush();action('directoryConfirm');await flush();assert(!requests.length);action('saveDirectories');await flush();r=take();respond(r,{detail:'目录配置已在其他页面更新'},409);await flush();assert(!node('cw-directory-dialog').open);assert(node('cw-message').textContent.includes('配置已在其他页面更新'));assert(!requests.length);
 configRevision=7;pick('transfer');await flush();r=take();respond(r,page([]));await flush();action('directoryRefresh');await flush();action('directoryConfirm');await flush();action('saveDirectories');await flush();r=take();assert.deepStrictEqual(JSON.parse(r.opt.body),{directories:{transfer:'0'},expected_revision:7});respond(r,{directories:{...directories('A').directories,transfer:{id:'0',name:'根目录'}},revision:8});await flush();configRevision=8;assert.strictEqual(node('cw-transfer-cid').value,'0');
 // Late browse and save responses cannot populate another account's fields.
 pick('source');await flush();stale=take();node('cw-account').value='B';node('cw-account').onchange();await flush();assert(!node('cw-directory-dialog').open);respond(stale,page([{id:'stale',name:'旧账号',is_dir:true}]));await flush();assert(node('cw-source-cid').value==='B1');
 pick('target');await flush();r=take();assert(r.url.includes('account_id=B'));respond(r,page([]));await flush();action('directoryConfirm');await flush();action('saveDirectories');await flush();stale=take();assert.strictEqual(stale.url,'/api/cloud/accounts/B/directories');node('cw-account').value='A';node('cw-account').onchange();await flush();const before=node('cw-target-cid').value;respond(stale,{directories:{source:{id:'B1',name:'B'},target:{id:'0',name:'B根目录'}},revision:99});await flush();assert.strictEqual(node('cw-target-cid').value,before);assert(!node('cw-directory-dialog').open);assert.strictEqual(requests.length,0);
 // A post-save rendering exception must never trap the native dialog in saving state.
 pick('source');await flush();r=take();respond(r,page([]));await flush();action('directoryConfirm');await flush();action('saveDirectories');await flush();r=take();
 const originalRender=node('cw-quality8-preview').replaceChildren;node('cw-quality8-preview').replaceChildren=function(){throw Error('render failed');};
 respond(r,{directories:{...directories('A').directories,source:{id:'0',name:'根目录'}},revision:9});await flush();
 assert(!node('cw-directory-dialog').open);assert(node('cw-message').textContent.includes('目录已保存'));node('cw-quality8-preview').replaceChildren=originalRender;
 pick('target');await flush();r=take();respond(r,page([]));await flush();assert(!button('Confirm').disabled);action('directoryCancel');
 console.log('PASS visible mobile navigation; inline directory pagination, string IDs, save/cancel/retry/conflict and account isolation');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
