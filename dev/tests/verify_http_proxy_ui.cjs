// Shipped service-config handlers with DOM substitutes.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/settings.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8');
const nodes={},calls=[];
const node=id=>nodes[id]||(nodes[id]={value:'',checked:false,disabled:false,textContent:'',style:{},classList:{remove(){}}});
const ctx=vm.createContext({console,Object,JSON,String,Number,Array,Promise,
 $:node,document:{querySelector(){return null;},getElementById:node},
 setInterval(){},setTimeout(){},toast(){},alert(){},icon(){return '';},loadDashboard(){},
 api:async(url,options)=>{calls.push({url,options});return {status:'success',message:'代理连接成功',config:{http_proxy_enabled:'1',http_proxy_url:'http://proxy.test:7890',http_proxy_username:'user',http_proxy_password:'********'}};}});
vm.runInContext(source.slice(source.indexOf('/* ═══════════ 服务配置')),ctx);
(async()=>{
 await ctx.loadConfig();
 assert(node('cfg-proxy-enabled').checked);assert.equal(node('cfg-proxy-password').value,'********');
 await ctx.testHttpProxy();
 assert.equal(calls.at(-1).url,'/api/config/test/proxy');assert(!node('proxyTestBtn').disabled);
 assert.equal(JSON.parse(calls.at(-1).options.body).http_proxy_password,'********');
 assert.equal(node('proxyTestResult').textContent,'代理连接成功');
 await ctx.testTmdb();assert.equal(JSON.parse(calls.at(-1).options.body).http_proxy_url,'http://proxy.test:7890');
 await ctx.testTelegram();assert.equal(JSON.parse(calls.at(-1).options.body).http_proxy_enabled,'1');
 node('cfg-proxy-enabled').checked=false;
 await ctx.saveConfig();
 const saved=calls.find(c=>c.url==='/api/config'&&c.options);
 assert.equal(JSON.parse(saved.options.body).http_proxy_enabled,'0');
 ctx.api=async()=>{throw Error('fixture offline');};await ctx.testHttpProxy();
 assert.equal(node('proxyTestResult').textContent,'fixture offline');assert(!node('proxyTestBtn').disabled);
 assert(html.includes('type="password" id="cfg-proxy-password"'));
 console.log('PASS proxy load/save, masked password, draft service tests and failed test button cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
