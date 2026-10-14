#!/usr/bin/env node
// P1 interface and directory selection invariants; not NAS/browser E2E.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const legacy = fs.readFileSync(path.join(root,'static/js/cloud-workbench.js'),'utf8');
const overlay = fs.readFileSync(path.join(root,'static/js/cloud-workbench-ui.js'),'utf8');
const index = fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const css = fs.readFileSync(path.join(root,'static/css/cloud-workbench-ui.css'),'utf8');
// The current Cookie workbench supersedes this historical, uninstalled overlay.
// Validate its real interactions instead of requiring the v1.3.1 injection.
if (!index.includes('/static/js/cloud-workbench-ui.js?v=') && legacy.includes('cw-ffprobe-enabled')) {
  assert(!index.includes('/static/css/cloud-workbench-ui.css?v='), 'old overlay must be fully disabled');
  const {spawnSync} = require('node:child_process');
  const result = spawnSync(process.execPath, [path.join(__dirname,'verify_my_drive_templates_ui.cjs')], {stdio:'inherit', cwd:root});
  if (result.status !== 0) process.exit(result.status || 1);
  console.log('PASS historical P1 compatibility: current Cookie workbench interaction suite');
  process.exit(0);
}
new vm.Script(overlay, {filename:'cloud-workbench-ui.js'});
assert(index.includes('/static/js/cloud-workbench-ui.js?v='), 'P1 script included in HTML');
assert(index.includes('/static/css/cloud-workbench-ui.css?v='), 'P1 stylesheet included in HTML');
assert(index.indexOf('/static/js/cloud-workbench.js?v=') < index.indexOf('/static/js/cloud-workbench-ui.js?v='), 'overlay must load after existing workbench');
assert(overlay.includes("api('/files?'"), 'workbench reads existing file API');
assert(overlay.includes('account_id:local.account'), 'folder browsing scoped to active account');
assert(overlay.includes('rev !== pickerVersion'), 'late response guard exists');
assert(overlay.includes('data-picker-action="confirm"'), 'explicit folder confirmation exists');
assert(css.includes('@media(max-width:600px)'), 'mobile layout exists');
const m = legacy.match(/  function selectDirectory\(role, folder, accountId\) \{[\s\S]*?\n  \}/);
assert(m, 'directory bridge must be installed after running apply_cloud_p1.py');
assert.equal(legacy.match(/function selectDirectory\(role, folder, accountId\)/g).length,1,'directory bridge must not duplicate');
const test = `(function(){var s={account:'A',source:null,target:null,transfer:null,epoch:0,plan:{},planRequest:0};let calls=0;function paths(){calls++}function msg(){} ${m[0]} return {s,selectDirectory,get calls(){return calls}}})()`;
const bridge = vm.runInNewContext(test);
bridge.selectDirectory('source',{id:'0',name:'根目录'},'A');
assert.equal(bridge.s.source.id,'0');
bridge.selectDirectory('target',{id:'123456',name:'电影'},'A');
assert.equal(bridge.s.source.id,'0','selecting target must not replace source');
assert.equal(bridge.s.target.id,'123456');
assert.equal(bridge.s.plan,null,'changing directory invalidates old preview');
assert.throws(() => bridge.selectDirectory('target',{id:'0',name:'根目录'},'A'),/不能相同/);
assert.throws(() => bridge.selectDirectory('target',{id:'9',name:'子目录',trailIds:['0','9']},'A'),/归档目录/);
const before = bridge.calls;
assert.throws(() => bridge.selectDirectory('transfer',{id:'111',name:'其他账号'},'B'),/账号已变化/);
assert.throws(() => bridge.selectDirectory('transfer',{id:'../../',name:'坏目录'},'A'),/目录 ID/);
assert.throws(() => bridge.selectDirectory('delete',{id:'11',name:'禁止'},'A'),/目录类型/);
assert.equal(bridge.calls,before,'failed selection should not update UI');
console.log('PASS P1 syntax, integration order, account isolation, preview invalidation, and mobile stylesheet');
