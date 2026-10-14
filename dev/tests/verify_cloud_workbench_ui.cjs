// Exercise the shipped workbench script; no live account or browser visual claims.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const nodes={},events={},requests=[],timers=[];
function node(id){return nodes[id]||(nodes[id]={dataset:{},value:'',textContent:'',innerHTML:'',hidden:false,checked:false,
  classList:{toggle(){}},replaceChildren(){this.innerHTML='';this.children=[];},children:[],append(x){this.children.push(x);},
  querySelectorAll(){return [];},addEventListener(name,fn){events[id+':'+name]=fn;}});}
const ctx=vm.createContext({console,URLSearchParams,Promise,Object,Array,Number,String,JSON,
  esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  document:{getElementById:node,createElement:()=>node('generated-'+Math.random())},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},
  showModalConfirm:async()=>true,
  fetch:(url,opt)=>new Promise(resolve=>requests.push({url,opt,resolve}))});
ctx.window=ctx;ctx.__activeTab='cloud';
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../static/js/cloud-workbench.js'),'utf8'),ctx);
function respond(request,body,status=200){request.resolve({ok:status<400,status,json:async()=>body});}
async function flush(){for(let i=0;i<15;i++)await Promise.resolve();}
function click(action){events['tab-cloud:click']({target:{closest:selector=>selector==='[data-cwa]'?{dataset:{cwa:action}}:null}});}
function tab(value){events['tab-cloud:click']({target:{closest:selector=>selector==='[data-cw-tab]'?{dataset:{cwTab:value}}:null}});}
// Drive the QR timer without awaiting its pending request.
async function run(){
  const loaded=ctx.cloudWorkbench.load();respond(requests.shift(),{accounts:[{id:'A',name:'甲'},{id:'B',name:'乙'}]});await loaded;
  tab('files');await flush();const first=requests.shift();
  node('cw-account').value='B';node('cw-account').onchange();await flush();
  respond(requests.shift(),{items:[{id:'2',name:'乙目录',is_dir:true}],total:1});await flush();
  respond(first,{items:[{id:'1',name:'甲旧目录',is_dir:true}],total:1});await flush();
  assert(node('cw-file-list').innerHTML.includes('乙目录')&&!node('cw-file-list').innerHTML.includes('甲旧目录'));
  assert(node('tab-cloud').innerHTML.includes('扫码登录 115'));
  assert(!/placeholder="[^"]*(?:UID|CID|SEID|AppID)/i.test(node('tab-cloud').innerHTML));
  node('cw-share').value='abcdefghi';node('cw-password').value='';click('preview');await flush();const share=requests.shift();
  node('cw-account').value='A';node('cw-account').onchange();await flush();respond(requests.shift(),{items:[],total:0});
  respond(share,{preview_id:'stale',items:[{id:'3',name:'旧账号分享'}],total:1});await flush();
  assert(!node('cw-share-list').innerHTML.includes('旧账号分享'));
  tab('accounts');node('cw-device').value='alipaymini';click('qr');await flush();respond(requests.shift(),{flow_id:'flow-1'});await flush();
  assert(!node('cw-qr').hidden);const polling=timers.at(-1).fn();await flush();
  respond(requests.shift(),{detail:'二维码已过期'},410);await polling;assert(node('cw-qr').hidden);
  const count=timers.length;click('qr');await flush();assert(requests[0].url==='/api/cloud/qr');
  respond(requests.shift(),{flow_id:'flow-2'});await flush();assert(timers.length===count+1);
  console.log('PASS cloud workbench account switching, stale directory/share responses, QR expiry/restart and no manual account IDs');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
