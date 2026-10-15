// Actual shipped receive handlers with controlled confirmation/network responses.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[],confirmations=[];let approval=true,modal=null,selected=['9'];
function node(id){return nodes[id]||(nodes[id]={id,value:'',textContent:'',innerHTML:'',children:[],checked:false,hidden:false,dataset:{},setAttribute(){},classList:{toggle(){}},replaceChildren(){this.children=[];this.innerHTML='';},append(n){this.children.push(n);},addEventListener(k,fn){events[id+':'+k]=fn;},querySelectorAll(){return id==='cw-share-list'?selected.map(i=>({dataset:{share:i}})):[];}});}
const good=body=>Promise.resolve({ok:true,json:async()=>body});
const context={console,URLSearchParams,Promise,Object,Array,Number,String,Math,JSON,esc:String,document:{getElementById:node,createElement:()=>node('new-'+Math.random())},setTimeout(){},clearTimeout(){},showModalConfirm:text=>{confirmations.push(text);return modal?new Promise(r=>modal.resolve=r):Promise.resolve(approval);},fetch(url,opt){
 if(url==='/api/cloud/status')return good({accounts:[{id:'A',uid:'1',name:'Account',authorization:{cookie:true}}]});
 if(url.endsWith('/directories'))return good({directories:{source:{id:'1',name:'来源'},target:{id:'2',name:'归档'},transfer:{id:'3',name:'转存'}},revision:2});
 if(url==='/api/cloud/settings')return good({settings:{}});
 if(url==='/api/cloud/rules'||url==='/api/cloud/jobs')return good({items:[]});
 if(url==='/api/cloud/jobs/job')return good({job:{kind:'transfer',state:'success',message:'已核实',result:{files:[]},steps:[]}});
 return new Promise(resolve=>requests.push({url,opt,resolve}));
}};context.window=context;context.__activeTab='cloud';vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/cloud-workbench.js','utf8'),context);
function click(action){events['tab-cloud:click']({target:{closest:s=>s==='[data-cwa]'?{dataset:{cwa:action}}:null}});}
async function flush(){for(let i=0;i<50;i++)await Promise.resolve();}
function take(url){let r=requests.shift();assert(r,'missing '+url);assert.strictEqual(r.url,url);return r;}
function reply(r,b,status=200){r.resolve({ok:status<400,status,json:async()=>b});}
async function run(){
 await context.cloudWorkbench.load();node('cw-share').value='https://115.com/s/abcdef123';node('cw-password').value='abcd';
 assert(!confirmations.length);
 node('cw-share').value='https://115.com/s/abcdef123';click('receiveLink');click('receiveLink');await flush();let r=take('/api/cloud/shares/receive'),body=JSON.parse(r.opt.body);assert(body.target_cid===undefined);assert(body.expected_revision===2&&body.confirmed&&body.account_id==='A');assert(/^[a-f0-9]{32}$/.test(body.request_id));assert(!requests.length);
 reply(r,{detail:'网络结果不明'},502);await flush();click('receiveLink');await flush();r=take('/api/cloud/shares/receive');assert.strictEqual(JSON.parse(r.opt.body).request_id,body.request_id);reply(r,{job_id:'job'});await flush();assert(node('cw-transfer-status').textContent.includes('已排队'));
 click('receiveLink');await flush();assert(!requests.length,'accepted intent opens original task, never another POST');
 node('cw-share').oninput();node('cw-share').value='https://115.com/s/abcdef124';click('preview');await flush();r=take('/api/cloud/shares/preview');reply(r,{preview_id:'preview',items:[{id:'9',name:'Movie'}],total:1});await flush();
 modal={};click('receive');click('receive');await flush();selected=['10'];modal.resolve(true);modal=null;await flush();assert(!requests.length);assert(node('cw-message').textContent.includes('选择已变化'));
 selected=['9'];click('receive');await flush();r=take('/api/cloud/transfers');assert.deepStrictEqual(JSON.parse(r.opt.body).file_ids,['9']);reply(r,{job_id:'job'});await flush();assert(!requests.length);
 console.log('PASS direct-link explicit submit, duplicate prevention, stable retry key, accepted task reuse and selection changes during confirmation');
}run().catch(e=>{console.error(e);process.exitCode=1;});
