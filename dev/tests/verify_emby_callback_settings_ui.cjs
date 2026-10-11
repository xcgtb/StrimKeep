const assert = require('assert'), vm = require('vm'), fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root,'static/js/settings.js'),'utf8');
const html = fs.readFileSync(path.join(root,'static/index.html'),'utf8');
for (const id of ['ingMode','ingReconcile','ingWebhookUrl','ingWebhookStatus','ingPollRow'])
  assert(html.includes('id="'+id+'"'));
const start = source.indexOf('async function loadIngest()');
const end = source.indexOf('/* ═══════════ 双库定时巡检',start);
const elements = {}, calls = [], toasts = [];
const $ = id => elements[id] || (elements[id] = {});
let failSave = false;
const ctx = { $,window:{location:{origin:'https://nas.example'}},encodeURIComponent,Date,parseInt,
  toast:(...a)=>toasts.push(a),
  api:async (url,opts)=>{
    if (opts) {
      calls.push(JSON.parse(opts.body));
      return failSave ? {status:'error',message:'not saved'} : {status:'success'};
    }
    if (url.endsWith('/status')) return {has_cache:false};
    return {status:'success',settings:{enabled:true,mode:'webhook',interval_min:5,reconcile_hours:24,webhook_token:'a b'},
      webhook:{pending:2,error:'等待重试'}};
  }};
vm.createContext(ctx); vm.runInContext(source.slice(start,end),ctx);
(async()=>{
  await ctx.loadIngest();
  assert.equal($('ingWebhookUrl').value,'https://nas.example/api/emby/webhook?token=a%20b');
  assert.equal($('ingPollRow').hidden,true);
  assert($('ingWebhookStatus').textContent.includes('待处理 2'));
  await ctx.saveIngest();
  assert.equal(calls[0].mode,'webhook'); assert.equal(calls[0].reconcile_hours,24);
  failSave=true; await ctx.saveIngest();
  assert(toasts.at(-1)[0].includes('保存失败'));
  console.log('PASS actual callback settings load/save, URL encoding, retry status and failed-save feedback');
})().catch(e=>{console.error(e);process.exitCode=1;});
