// Webhook token protection, real shipped UI functions under a VM fixture.
const assert = require('assert'), vm = require('vm'), fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/settings.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
for (const id of ['ingWebhookGenerateBtn', 'ingWebhookResetBtn', 'ingWebhookResetConfirmBtn']) {
  assert(html.includes(id) || source.includes(id));
}
const start = source.indexOf('async function loadIngest()');
const end = source.indexOf('/* ═══════════ 双库定时巡检', start);
const elements = {}, calls = [], messages = [];
const $ = id => elements[id] || (elements[id] = {style:{},disabled:false,hidden:false,value:''});
let token = 'existing-token', mode = 'webhook', modal = '';
const context = { $, window:{location:{origin:'https://nas.example'}}, encodeURIComponent, Date, parseInt,
  toast:(...a)=>messages.push(a), openModal:html=>{modal=html;},closeModal:()=>{modal='';},
  api:async (url, opts)=>{
    if (!opts) {
      if (url.endsWith('/status')) return {has_cache:false};
      return {status:'success', settings:{enabled:true, mode, interval_min:5, reconcile_hours:24, webhook_token:token}, webhook:{pending:0}};
    }
    const b = JSON.parse(opts.body); calls.push(b);
    if (b.generate_token) {
      if (token) throw Error('409 existing token');
      token='first-token';
    }
    if (b.reset_token) {
      if (b.confirm_reset !== 'RESET_WEBHOOK_TOKEN') throw Error('400 confirmation required');
      token='reset-token';
    }
    if (b.mode) mode = b.mode;
    return {status:'success'};
  }};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
(async()=>{
  await context.loadIngest();
  assert.equal($('ingWebhookGenerateBtn').hidden, true);
  assert.equal($('ingWebhookResetBtn').hidden, false);
  await context.generateIngestCallback();
  assert.equal(calls.length, 0, 'existing token cannot be regenerated');
  context.requestResetIngestCallback();
  assert(modal.includes('原令牌和旧回调地址立即失效'));
  assert(modal.includes('输入「重置」'));
  $('ingWebhookResetConfirm').value='不重置';
  context.checkIngestResetConfirm();
  assert.equal($('ingWebhookResetConfirmBtn').disabled,true);
  await context.resetIngestCallback();
  assert.equal(calls.length,0,'reset refused without second confirmation');
  $('ingWebhookResetConfirm').value='重置';
  context.checkIngestResetConfirm();
  assert.equal($('ingWebhookResetConfirmBtn').disabled,false);
  await context.resetIngestCallback();
  assert.equal(calls.length,1);
  assert.equal(calls[0].reset_token,true);
  assert.equal(calls[0].confirm_reset,'RESET_WEBHOOK_TOKEN');
  assert.equal(token,'reset-token');
  assert.equal(modal,'');
  token=''; await context.loadIngest();
  assert.equal($('ingWebhookGenerateBtn').hidden,false);
  assert.equal($('ingWebhookResetBtn').hidden,true);
  await context.generateIngestCallback();
  assert.equal(calls[1].generate_token,true);
  assert.equal(token,'first-token');
  await context.generateIngestCallback();
  assert.equal(calls.length,2,'cannot regenerate once token exists');
  console.log('PASS webhook generate/reset split, two-step confirmation, stale-token guards');
})().catch(e=>{console.error(e);process.exitCode=1;});
