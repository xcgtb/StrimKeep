// Scope isolation and asynchronous draft preservation in the shipped handlers.
const vm=require('vm'),assert=require('assert'),{asset}=require('./frontend_source.cjs');
const nodes={},requests=[];
const $=id=>nodes[id]||(nodes[id]={value:'',type:'text',checked:false,disabled:false,textContent:'',classList:{remove(){}},closest(){return null;}});
const c=vm.createContext({console,JSON,String,Object,Array,Promise,$,document:{querySelector(){return null;}},revealed:{},icon:()=>'',setInterval(){},toast(){},loadBotStatus(){},api:(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve}))});
c.window={addEventListener(){}};
vm.runInContext(asset('/static/js/settings.js'),c);
async function flush(){for(let i=0;i<10;i++)await Promise.resolve();}
(async()=>{
 $('cfg-emby-key').value='fixture***';$('cfg-emby-host').value='http://emby.test:8096';$('cfg-tmdb-key').value='unsaved-tmdb';$('cfg-tg-chat').value='unsaved-chat';
 c.saveServiceConfig('emby');c.saveServiceConfig('emby');assert.equal(requests.length,1);
 let r=requests.shift();assert.deepEqual(Object.keys(JSON.parse(r.opts.body)).sort(),['emby_host','emby_key','emby_local_path','emby_share_path']);
 assert.equal(JSON.parse(r.opts.body).emby_key,'fixture***');
 $('cfg-emby-host').value='new-draft-during-save';
 r.resolve({status:'success',config:{emby_host:'http://saved.test',emby_key:'saved***',tmdb_key:'should-not-replace'}});await flush();
 assert.equal($('cfg-emby-host').value,'new-draft-during-save');assert.equal($('cfg-emby-key').value,'saved***');
 assert.equal($('cfg-tmdb-key').value,'unsaved-tmdb');assert.equal($('cfg-tg-chat').value,'unsaved-chat');assert(!$('settingsSave-emby').disabled);
 c.loadServiceConfig('tmdb');requests.shift().resolve({status:'success',config:{tmdb_key:'read***',telegram_chat_id:'saved-other'}});await flush();
 assert.equal($('cfg-tmdb-key').value,'read***');assert.equal($('cfg-tg-chat').value,'unsaved-chat');
 c.saveServiceConfig('telegram');r=requests.shift();assert(!('http_proxy_url' in JSON.parse(r.opts.body)));
 r.resolve({status:'error',message:'fixture failure'});await flush();assert(!$('settingsSave-telegram').disabled);assert($('settingsStatus-telegram').textContent.includes('fixture failure'));
 $('cfg-proxy-enabled').type='checkbox';$('cfg-proxy-enabled').checked=true;$('cfg-proxy-password').value=' secret with spaces ';
 const body=c.settingsScopedBody('proxy');assert.equal(body.http_proxy_enabled,'1');assert.equal(body.http_proxy_password,' secret with spaces ');assert.equal(Object.keys(body).length,4);
 const calls=[];c.tgtoOpen=s=>calls.push(s);c.hydratePosters=()=>{};c.document.addEventListener=()=>{};
 vm.runInContext(asset('/static/js/dashboard.js'),c);
 c.overviewHot.rows.movie=[{title:'电影',type:'movie',tmdb_id:'123',poster:'/api/tmdb/poster/a.jpg'}];c.openOverviewRecommendation(0);
 assert.equal(calls[0].media_type,'movie');assert.equal(calls[0].tmdb_id,'123');
 c.overviewHot.media='tv';c.overviewHot.rows.tv=[{title:'剧',type:'tv',tmdb_id:'456'}];c.openOverviewRecommendation(0);assert.equal(calls[1].media_type,'tv');
 console.log('PASS scoped save/reload, masks, preserved other drafts and in-flight edits, duplicate guard, failure cleanup; movie/TV posters share resource entry');
})().catch(e=>{console.error(e);process.exitCode=1;});
