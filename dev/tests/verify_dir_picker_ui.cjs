// Interaction checks in a minimal DOM stub; this is not browser visual testing.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const path = require('path');
const html = require('./frontend_source.cjs').loadFrontend();
const start = html.indexOf('var __emptyDirsPreview = []');
const end = html.indexOf('async function scanEmptyDirs()', start);
const elements = new Map();
for (const id of ['emptyDirBrowserBody', 'modalBg', 'emptyDirPath', 'emptyDirScope', 'emptyDirSelectedPath',
                  'emptyDirChooseBtn', 'emptyDirScanBtn', 'cleanEmptyBtn', 'emptyDirsStats',
                  'emptyDirsList', 'emptyDirBrowserFilter']) {
  elements.set(id, {innerHTML:'', textContent:'', value:'', disabled:true,
                    style:{}, classList:{contains:()=>true}});
}
let pending = [], closed = 0, scans = 0;
const ctx = vm.createContext({
  $:id=>elements.get(id), encodeURIComponent,
  esc:s=>String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
                                        '"':'&quot;',"'":'&#39;'}[c])),
  openModal:()=>{}, closeModal:()=>closed++, scanEmptyDirs:()=>scans++,
  api:url=>new Promise(resolve=>pending.push({url, resolve}))
});
vm.runInContext(html.slice(start, end), ctx);
const roots = [{lib:'local',name:'本地库',path:'/media/local',available:true},
               {lib:'share',name:'分享库',path:'/media/share',available:false,message:'missing'}];
const result = {status:'success', roots, lib:'local', path:'/media/local/剧集',
  breadcrumbs:[{name:'本地库',path:'/media/local'},{name:'剧集',path:'/media/local/剧集'}],
  items:[{name:'"<字幕>"',path:'/media/local/剧集/"<字幕>"'}],
  total:201,offset:0,limit:200,has_more:true,search:''};

async function main(){
  ctx.renderEmptyDirBrowser(result);
  let rendered = elements.get('emptyDirBrowserBody').innerHTML;
  assert(rendered.includes('&quot;&lt;字幕&gt;&quot;'));
  assert(!rendered.includes('openEmptyDirChild(\'') && rendered.includes('openEmptyDirChild(0)'));
  assert(rendered.includes('下一页') && rendered.includes('（不可访问）'));
  assert.strictEqual((rendered.match(/>本地库</g)||[]).length,1);
  assert(rendered.includes('当前位置') && rendered.includes('库根目录'));
  console.log('PASS names escaped and navigation does not embed raw paths in handlers');

  ctx.__emptyDirBrowser.data = result;
  ctx.openEmptyDirChild(0);
  assert(pending[0].url.includes(encodeURIComponent(result.items[0].path)));
  pending.shift().resolve(result); await new Promise(resolve=>setImmediate(resolve));
  ctx.pageEmptyDirBrowser(1);
  assert(pending[0].url.includes('offset=200'));
  pending.shift().resolve(result); await new Promise(resolve=>setImmediate(resolve));
  console.log('PASS child navigation and pagination request correct scope');

  const first = ctx.loadEmptyDirBrowser('/media/local',0,'');
  const second = ctx.loadEmptyDirBrowser(result.path,0,'');
  pending[1].resolve(result); await second;
  pending[0].resolve({...result,path:'/media/local'}); await first;
  pending=[];
  assert.strictEqual(ctx.__emptyDirBrowser.data.path, result.path);
  console.log('PASS late response cannot replace newer directory selection');

  ctx.__emptyDirsPreview = [{path:'/old'}];
  ctx.useEmptyDirSelection(true);
  assert.strictEqual(elements.get('emptyDirPath').value, result.path);
  assert.strictEqual(elements.get('emptyDirScope').value, 'directory');
  assert.strictEqual(elements.get('emptyDirScanBtn').disabled,false);
  assert.strictEqual(ctx.__emptyDirsPreview.length,0);
  assert.strictEqual(closed,1); assert.strictEqual(scans,0);
  assert.strictEqual(elements.get('cleanEmptyBtn').style.display,'none');
  assert(!rendered.includes('选中并预览扫描') && rendered.includes('确认选择'));
  console.log('PASS selection clears old preview and enables preview button without auto-scanning');

  assert.strictEqual(ctx.emptyDirFileSummary({file_count:20,extensions:{'.ass':20}}),
                     '附属文件 20 个 · 字幕 20 个');
  assert.strictEqual(ctx.emptyDirFileSummary({file_count:0}), '目录内没有文件');
  console.log('PASS subtitle counts distinguished from genuinely empty directories');
  console.log('PASS 5/5; DOM-stub behavior checks only');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
