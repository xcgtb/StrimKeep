// Behavior checks with a minimal DOM stub; not browser visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const ids=['emptyDirBrowserBody','modalBg','emptyDirScope','emptyDirPath','emptyDirSelectedPath','emptyDirChooseBtn','emptyDirScanBtn','cleanEmptyBtn','emptyDirsStats','emptyDirsList'];
const nodes=new Map(ids.map(id=>[id,{value:'',textContent:'',innerHTML:'',disabled:true,style:{},classList:{contains:()=>true}}]));
let pending=[],closed=0,toasts=[];
const ctx=vm.createContext({$:id=>nodes.get(id),encodeURIComponent,openModal:()=>{},closeModal:()=>closed++,
 esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 api:(url,opts)=>new Promise(resolve=>pending.push({url,opts,resolve})),toast:(...a)=>toasts.push(a)});
vm.runInContext(html.slice(html.indexOf('var __emptyDirsPreview = []'),html.indexOf('async function cleanEmptyDirs()')),ctx);
const roots=[{lib:'local',name:'本地库',path:'/media/local',available:true},{lib:'share',name:'分享库',path:'/media/share',available:true}];
const all={status:'success',path:'',roots};
const local={status:'success',path:'/media/local/剧集',lib:'local',roots,breadcrumbs:[{name:'本地库',path:'/media/local'},{name:'剧集',path:'/media/local/剧集'}],items:[],total:0};
const item=lib=>({lib,path:'/media/'+lib+'/剧集/Show {tmdb-1}',rel:'剧集/Show {tmdb-1}',abs_path:'/media/'+lib+'/剧集/Show {tmdb-1}',file_count:1,extensions:{'.ass':1},type:'附属文件残留',rule:'known'});
const result={status:'success',lib:'all',hits:2,folders:8,empty_dirs:2,preview_limit:100,preview:[item('local'),item('share')],scopes:[{lib:'local',hits:1},{lib:'share',hits:1}]};
async function main(){
 ctx.renderEmptyDirBrowser(local);let rendered=nodes.get('emptyDirBrowserBody').innerHTML;
 assert.strictEqual((rendered.match(/>本地库</g)||[]).length,1);
 assert.strictEqual((rendered.match(/>分享库</g)||[]).length,1);
 assert(rendered.includes('整个媒体库')&&rendered.includes('库根目录')&&rendered.includes('aria-current="location">剧集'));
 console.log('PASS distinct whole-library/local/share choices and nonduplicate breadcrumb labels');
 ctx.__emptyDirBrowser.data=all;ctx.useEmptyDirSelection(false);
 assert.strictEqual(nodes.get('emptyDirScope').value,'all');assert.strictEqual(nodes.get('emptyDirPath').value,'');
 let task=ctx.scanEmptyDirs(),req=pending.shift();
 assert.deepStrictEqual(JSON.parse(req.opts.body),{path:'',scope:'all',limit:100});
 req.resolve(result);await task;
 rendered=nodes.get('emptyDirsList').innerHTML;
 assert(rendered.includes('本地库')&&rendered.includes('分享库'));
 assert(!rendered.includes('预览 115 残留'));
 assert(nodes.get('emptyDirsStats').innerHTML.includes('分享库命中'));
 console.log('PASS whole-library request and per-item library labels without a separate cloud button');
 ctx.__emptyDirBrowser.data=local;ctx.useEmptyDirSelection(false);
 assert.strictEqual(ctx.__emptyDirsPreview.length,0);
 task=ctx.scanEmptyDirs();req=pending.shift();
 assert.deepStrictEqual(JSON.parse(req.opts.body),{path:local.path,scope:'directory',limit:100});
 req.resolve({...result,lib:'local',preview:[item('local')]});await task;
 console.log('PASS directory selection clears old combined preview and sends exact scope');
 const ongoing=ctx.scanEmptyDirs(),ongoingReq=pending.shift();
 assert(nodes.get('emptyDirChooseBtn').disabled && nodes.get('emptyDirScanBtn').disabled);
 await ctx.scanEmptyDirs();assert.strictEqual(pending.length,0);
 ctx.__emptyDirBrowser.data=all;ctx.useEmptyDirSelection(false);
 assert.strictEqual(nodes.get('emptyDirPath').value,local.path);
 ongoingReq.resolve({...result,preview:[item('local')]});await ongoing;
 assert(!nodes.get('emptyDirChooseBtn').disabled && !nodes.get('emptyDirScanBtn').disabled);
 console.log('PASS scan disables duplicate submissions and scope changes until completion');
 task=ctx.scanEmptyDirs();req=pending.shift();req.resolve({status:'error',message:'分享库：missing'});await task;
 assert.strictEqual(ctx.__emptyDirsPreview.length,0);
 assert.strictEqual(nodes.get('cleanEmptyBtn').style.display,'none');
 assert(nodes.get('emptyDirsList').innerHTML.includes('分享库：missing'));
 console.log('PASS failed scan clears cleanup targets and displays reason');
 console.log('PASS 5/5; DOM-stub behavior checks only');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
