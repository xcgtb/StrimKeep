// Shipped handlers with controlled requests; no browser visual claim.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[],failures=[];let approve=true;
function node(id){return nodes[id]||(nodes[id]={id,value:'',checked:false,textContent:'',innerHTML:'',children:[],dataset:{},append(v){this.children.push(v);},prepend(v){this.children.unshift(v);},replaceChildren(){this.children=[];this.innerHTML='';},addEventListener(k,fn){events[this.id+':'+k]=fn;},querySelectorAll(){return [];}});}
const state={account:'A',epoch:1,dirRevision:2,plan:'old',planRequest:1};
const api=(url,body)=>new Promise((resolve,reject)=>requests.push({url,body,resolve,reject}));
const context={console,URLSearchParams,Array,Object,Number,Date,String,JSON,Promise,document:{getElementById:node,createElement:()=>node('new-'+Math.random())},showModalConfirm:async()=>approve};context.window=context;vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/cloud-linkage.js','utf8'),context);
context.cloudLinkage.mount({state,api,guard:fn=>Promise.resolve().then(fn).catch(e=>failures.push(e.message)),schedule:()=>({schedule_mode:'cron',cron_expression:'0 * * * *',timezone:'Asia/Shanghai'}),namingSaved:()=>true,rules:async()=>{},saveNaming:async()=>{}});
function click(action,container='cw-link-reader'){events[container+':click']({target:{closest:sel=>sel==='[data-link-action]'?{dataset:{linkAction:action}}:null}});}
function special(key,id,container){events[container+':click']({target:{closest:sel=>sel==='[data-'+key+']'?{dataset:{[key.replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:id}}:null}});}
async function flush(){for(let i=0;i<40;i++)await Promise.resolve();}
function take(url){let r=requests.shift();assert(r,'missing '+url);assert.strictEqual(r.url,url);return r;}
async function run(){
 const linkage=context.cloudLinkage;
 linkage.show('channels');await flush();take('/accounts/A/telegram-reader').resolve({revision:2,connected:false,pending:false});await flush();
 node('cw-link-api-id').value='123';node('cw-link-api-hash').value='a'.repeat(32);node('cw-link-phone').value='+8613800000000';click('sendCode');click('sendCode');await flush();let r=take('/accounts/A/telegram-reader');assert(r.body.action==='code'&&r.body.expected_revision===2);assert(!requests.length);r.resolve({revision:3,pending:true});await flush();assert.strictEqual(node('cw-link-api-hash').value,'');
 node('cw-link-code').value='12345';click('verifyLogin');await flush();r=take('/accounts/A/telegram-reader');assert(r.body.code==='12345');r.resolve({revision:4,needs_password:true,pending:true});await flush();assert(node('cw-link-reader-status').textContent.includes('两步验证'));
 node('cw-link-channel-mode').value='user';node('cw-link-history-limit').value='120';node('cw-link-poll-seconds').value='60';assert.strictEqual(linkage.channelFields().history_limit,120);
 linkage.channelEdit({mode:'bot',history_limit:0,poll_seconds:90});assert.strictEqual(node('cw-link-poll-seconds').value,90);
 linkage.show('organize');await flush();take('/accounts/A/organizer-sources').resolve({revision:2,items:[{id:'3',name:'<频道>',roles:['频道']}],target:{id:'2',name:'归档'}});await flush();assert(node('cw-link-sources').innerHTML.includes('&lt;频道&gt;'));
 node('cw-link-sources').querySelectorAll=()=>[{dataset:{intakeId:'3'}}];approve=false;click('saveSources','cw-link-intake');await flush();assert(!requests.length);approve=true;click('saveSources','cw-link-intake');await flush();r=take('/accounts/A/organizer-sources');assert.deepStrictEqual(Array.from(r.body.source_ids),['3']);assert.strictEqual(r.body.cron_expression,'0 * * * *');r.resolve({ids:['r'],next_run:123});await flush();
 linkage.show('playback');await flush();take('/accounts/A/emby-connections').resolve({revision:1,items:[{id:'c',name:'<影院>',url:'http://emby',enabled:true,notify:true,personal_root:'/strm',share_root:'/share'}]});take('/accounts/A/ingestion').resolve({items:[{id:'i',asset_id:'a',state:'pending',result:{message:'等待'}}]});await flush();assert(node('cw-link-connections').innerHTML.includes('&lt;影院&gt;'));assert(node('cw-link-ingestion').innerHTML.includes('等待入库'));
 special('emby-edit','c','cw-link-emby');assert.strictEqual(node('cw-link-emby-key').value,'');node('cw-link-emby-name').value='New';click('saveEmby','cw-link-emby');await flush();r=take('/accounts/A/emby-connections');assert.strictEqual(r.body.connection.id,'c');assert.strictEqual(r.body.connection.key,'');r.resolve({});await flush();take('/accounts/A/emby-connections').resolve({revision:2,items:[]});await flush();
 special('emby-test','c','cw-link-emby');await flush();r=take('/accounts/A/emby-connections/c/test');assert(r.body,'test is POST');r.resolve({name:'Emby',version:'4'});await flush();assert(node('cw-link-emby-status').textContent.includes('连接成功'));
 linkage.show('channels');await flush();r=take('/accounts/A/telegram-reader');linkage.reset();state.account='B';state.epoch++;r.resolve({revision:99,connected:true});await flush();assert(!node('cw-link-reader-status').textContent.includes('已连接'));assert.strictEqual(node('cw-link-emby-url').value,'');assert.strictEqual(node('cw-link-code').value,'');
 assert(!failures.length,failures.join(','));assert(!requests.length);
 console.log('PASS v1.3.6 login single submit/secrets/2FA, channel history, intake schedule confirmation, Emby key preservation/POST test, safe rendering and account isolation');
}run().catch(e=>{console.error(e);process.exitCode=1;});
