// Actual shipped handlers in a DOM substitute: selection, confirmation, TMDB, stale replies.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[],confirms=[];let approve=false,selected=['4'];
function node(id){return nodes[id]||(nodes[id]={dataset:{},value:'',textContent:'',innerHTML:'',hidden:false,checked:false,children:[],setAttribute(){},classList:{toggle(){}},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},addEventListener(k,fn){events[id+':'+k]=fn;},querySelectorAll(){return id==='cw-share-list'?selected.map(id=>({dataset:{share:id}})):[];}});}
const good=body=>Promise.resolve({ok:true,json:async()=>body});
const context={console,URLSearchParams,Promise,Object,Array,Number,String,JSON,esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),document:{getElementById:node,createElement:()=>node('new-'+Math.random())},setTimeout(){},clearTimeout(){},showModalConfirm:async text=>{confirms.push(text);return approve;},fetch(url,opt){
 if(url==='/api/cloud/status')return good({accounts:[{id:'A',uid:'123',name:'测试',authorization:{cookie:true}}]});
 if(url.endsWith('/directories'))return good({directories:{source:null,target:null,transfer:null,unknown:null,strm:null},revision:0});
 if(url==='/api/cloud/settings'&&!opt.method)return good({settings:{share_output_root:'/media/share',share_emby_output_root:'/share',output_root:'/media/local'}});
 if(url==='/api/cloud/jobs')return good({items:[]});
 if(url==='/api/cloud/jobs/job')return good({job:{state:'success',kind:'share_strm',message:'完成',result:{generated:[{path:'/media/share/new.strm'}]},steps:[]}});
 return new Promise(resolve=>requests.push({url,opt,resolve}));
}};context.window=context;context.__activeTab='cloud';vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/cloud-workbench.js'),'utf8'),context);
async function flush(){for(let i=0;i<40;i++)await Promise.resolve();}
function click(key){events['tab-cloud:click']({target:{closest:s=>s==='[data-cwa]'?{dataset:{cwa:key}}:null}});}
function reply(r,body,status=200){r.resolve({ok:status<400,status,json:async()=>body});}
function take(url){const r=requests.shift();assert(r,'missing '+url);assert.strictEqual(r.url,url);return r;}
const plan={id:'plan',output_root:'/media/share',effect:'仅写NAS',items:[{source:{id:'4',name:'<Movie>.mkv'},media:{directory:'分享-namespace/Movie',name:'Movie.mkv'}}],skipped:[{file_id:'5',name:'Unknown.mkv',reason:'需修正'}],filtered:[]};
async function initial(){node('cw-share').value='https://115.com/s/sharecode';node('cw-password').value='password';click('preview');await flush();reply(take('/api/cloud/shares/preview'),{preview_id:'preview',items:[{id:'4',name:'<Movie>.mkv'}],total:1});await flush();}
async function preview(){click('shareStrmPreview');await flush();const r=take('/api/cloud/shares/strm/preview');assert.deepStrictEqual(JSON.parse(r.opt.body).file_ids,['4']);reply(r,{plan:JSON.parse(JSON.stringify(plan))});await flush();}
async function run(){
 await context.cloudWorkbench.load();assert(node('tab-cloud').innerHTML.includes('分享库 STRM 输出目录'));
 await initial();await preview();assert(node('cw-share-strm-plan').innerHTML.includes('&lt;Movie>'));assert(!node('cw-share-strm-plan').innerHTML.includes('<Movie>'));
 click('shareStrmApply');await flush();assert(!requests.length);assert(confirms.at(-1).includes('不转存'));assert(confirms.at(-1).includes('分享取消'));
 selected=['9'];click('shareStrmApply');await flush();assert(!requests.length);assert(node('cw-message').textContent.includes('选择已变化'));selected=['4'];
 node('cw-share-correction').value='4';node('cw-share-kind').value='movie';node('cw-share-query').value='Movie';click('shareSearch');await flush();let r=take('/api/cloud/tmdb?query=Movie&kind=movie');reply(r,{items:[{id:44,title:'Movie',release_date:'2024'}]});await flush();node('cw-share-matches').children[0].onclick();assert(node('cw-share-strm-plan').innerHTML==='');
 click('shareStrmPreview');await flush();r=take('/api/cloud/shares/strm/preview');assert.deepStrictEqual(JSON.parse(r.opt.body).overrides,{'4':{kind:'movie',tmdb_id:'44'}});reply(r,{plan:JSON.parse(JSON.stringify(plan))});await flush();
 approve=true;click('shareStrmApply');click('shareStrmApply');await flush();r=take('/api/cloud/shares/strm/apply');assert.deepStrictEqual(JSON.parse(r.opt.body),{plan_id:'plan',confirmed:true});assert(!requests.length);reply(r,{job_id:'job'});await flush();assert(node('cw-job-summary').children.some(n=>n.textContent.includes('已生成')));
 await initial();click('shareStrmPreview');await flush();r=take('/api/cloud/shares/strm/preview');node('cw-account').value='B';node('cw-account').onchange();await flush();reply(r,{plan:JSON.parse(JSON.stringify(plan))});await flush();assert(node('cw-share-strm-plan').innerHTML==='');click('shareStrmApply');await flush();assert(!requests.length);
 await initial();await preview();node('cw-share').value='newlink';node('cw-share').oninput();click('shareStrmApply');await flush();assert(!requests.length);assert(node('cw-share-strm-plan').innerHTML==='');
 console.log('PASS share-library preview escaping, separate TMDB corrections, read-only effect confirmation, duplicate prevention, changed selection/link and stale account responses');
}run().catch(error=>{console.error(error);process.exitCode=1;});
