// Shipped handlers: compact policy edits, real tree paging and automatic schedule names.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[],writes=[];
function node(id){return nodes[id]||(nodes[id]={id,dataset:{},value:'',checked:false,hidden:false,disabled:false,textContent:'',innerHTML:'',children:[],classList:{toggle(){}},setAttribute(){},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},insertAdjacentHTML(position,html){this.innerHTML+=html;},querySelectorAll(){return [];},addEventListener(name,fn){events[id+':'+name]=fn;}});}
const ok=body=>Promise.resolve({ok:true,json:async()=>body});
const context={console,URLSearchParams,Promise,Object,Array,Number,String,JSON,Math,Set,esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),document:{getElementById:node,createElement:()=>node('new-'+Math.random())},setTimeout(){},clearTimeout(){},showModalConfirm:async()=>true,fetch(url,opt){
 if(url==='/api/cloud/status')return ok({accounts:[{id:'A',uid:'123',name:'测试',authorization:{cookie:true}}]});
 if(url.endsWith('/directories'))return ok({revision:1,directories:{source:{id:'1',name:'待整理'},target:{id:'2',name:'归档'},strm:{id:'2',name:'影视库'}}});
 if(url==='/api/cloud/settings'&&!opt.method)return ok({settings:{}});
 if(url==='/api/cloud/settings'&&opt.method){writes.push({url,body:JSON.parse(opt.body)});return ok({});}
 if(url==='/api/cloud/rules'&&!opt.method)return ok({items:[]});
 if(url.startsWith('/api/cloud/files?')&&new URLSearchParams(url.split('?')[1]).get('cid')==='0')return ok({items:[{id:'1',name:'目录',is_dir:true},{id:'9',name:'<movie>.mkv',size:1073741824,is_dir:false}],total:2,has_more:false});
 return new Promise(resolve=>requests.push({url,opt,resolve}));
}};context.window=context;vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/cloud-workbench.js','utf8'),context);
async function flush(){for(let i=0;i<40;i++)await Promise.resolve();}
function click(key){events['tab-cloud:click']({target:{closest:q=>q==='[data-cwa]'?{dataset:{cwa:key}}:null}});}
function tab(key){events['tab-cloud:click']({target:{closest:q=>q==='[data-cw-tab]'?{dataset:{cwTab:key}}:null}});}
function quality(data){node('cw-quality8-rules').onclick({target:{closest:()=>({tagName:'BUTTON',dataset:{qualityIndex:'0',...data}})}});}
function response(r,body,status=200){r.resolve({ok:status<400,status,json:async()=>body});}
function branch(cid){const body=node('body-'+cid),entries=node('entries-'+cid),status=node('status-'+cid),more=node('more-'+cid);body.querySelector=q=>q==='.cw-tree-entries'?entries:q==='.cw-tree-status'?status:more;const b={dataset:{treeCid:cid},open:true,isConnected:true,querySelector:()=>body};more.closest=()=>b;return {b,entries,status,more};}
(async()=>{
 await context.cloudWorkbench.load();tab('organize');await flush();
 const html=node('tab-cloud').innerHTML,policy=node('cw-quality8-rules');
 assert(html.includes('type="hidden" id="cw-rule-name"'));assert(html.includes('type="hidden" id="cw-sync-rule-name"'));
 assert.strictEqual((policy.innerHTML.match(/class="cw-quality-rule/g)||[]).length,8);assert(!policy.innerHTML.includes('<textarea'));assert(policy.innerHTML.includes('设置顺序'));assert(policy.innerHTML.includes('已启用'));
 quality({qualityOpen:'1'});await flush();assert(policy.innerHTML.includes('新增发布组'));node('cw-quality-group-0').value='<WF>';quality({qualityAdd:'1'});await flush();assert(policy.innerHTML.includes('&lt;WF>'));
 quality({qualityIndex:'1',qualityTier:'0:1'});await flush();
 node('cw-quality8-rules').onchange({target:{dataset:{qualityIndex:'0',qualityField:'enabled'},checked:true}});quality({qualityMove:'1'});await flush();
 click('saveNaming');await flush();let saved=writes.at(-1).body;
 assert.strictEqual(saved.quality8_rules[0].key,'source');assert.strictEqual(saved.quality8_rules[0].tiers[0],'Remux');assert.strictEqual(saved.quality8_rules[0].tiers.at(-1),'其他 / 未识别');
 assert.strictEqual(saved.quality8_rules[1].groups[0],'<WF>');assert(saved.quality8_rules[1].enabled);assert(!JSON.stringify(saved).includes('__open'));
 const before=JSON.stringify(saved.quality8_rules[0].tiers);quality({qualityIndex:'0',qualityTier:(saved.quality8_rules[0].tiers.length-1)+':-1'});await flush();click('saveNaming');await flush();assert.strictEqual(JSON.stringify(writes.at(-1).body.quality8_rules[0].tiers),before,'unknown tier cannot move');
 click('saveRule');await flush();let r=requests.shift();assert(r.url==='/api/cloud/rules');assert.strictEqual(JSON.parse(r.opt.body).name,'影视整理 · 待整理');response(r,{next_run:1792047600});await flush();
 tab('files');await flush();assert(node('cw-file-list').innerHTML.includes('&lt;movie>.mkv'));assert(node('cw-file-list').innerHTML.includes('1.0 GB'));assert(!node('cw-file-list').innerHTML.includes('<movie>'));
 const tree=branch('1');events['cw-file-list:toggle']({target:tree.b});events['cw-file-list:toggle']({target:tree.b});await flush();assert.strictEqual(requests.length,1,'opening a folder requests one page only');r=requests.shift();assert(r.url.endsWith('offset=0'));
 response(r,{items:Array.from({length:100},(_,i)=>({id:String(100+i),name:'E'+i+'.mkv',size:1024,is_dir:false})),total:150,has_more:true});await flush();assert(!requests.length);assert(!tree.more.hidden);assert(tree.entries.innerHTML.includes('E99.mkv'));
 events['cw-file-list:click']({target:{closest:()=>tree.more}});await flush();r=requests.shift();assert(r.url.endsWith('offset=100'));response(r,{detail:'网络暂不可用'},502);await flush();assert(tree.status.textContent.includes('网络暂不可用'));assert.strictEqual(tree.b.dataset.offset,'100');assert(tree.entries.innerHTML.includes('E99.mkv'));
 events['cw-file-list:click']({target:{closest:()=>tree.more}});await flush();r=requests.shift();assert(r.url.endsWith('offset=100'));response(r,{items:[{id:'250',name:'Subtitle.srt',size:123,is_dir:false},{id:'251',name:'子目录',is_dir:true}],total:102,has_more:false});await flush();assert(tree.more.hidden);assert(tree.entries.innerHTML.includes('E99.mkv')&&tree.entries.innerHTML.includes('Subtitle.srt')&&tree.entries.innerHTML.includes('子目录'));
 const stale=branch('7');events['cw-file-list:toggle']({target:stale.b});await flush();r=requests.shift();node('cw-account').value='B';node('cw-account').onchange();await flush();response(r,{items:[{id:'stale',name:'旧账号文件',is_dir:false}],total:1,has_more:false});await flush();assert(!stale.entries.innerHTML.includes('旧账号文件'));
 assert(!requests.length);console.log('PASS compact eight-dimension editor, tier/group order and unknown-last guards, automatic names, full file tree pages/retry/string IDs and account isolation');
})().catch(e=>{console.error(e);process.exitCode=1;});
