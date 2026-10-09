// Interaction regression checks with a minimal DOM; not browser visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const ids=['emptyDirBrowserBody','modalBg','emptyDirScope','emptyDirPath','emptyDirSelectedPath','emptyDirChooseBtn','emptyDirScanBtn','cleanEmptyBtn','emptyDirsStats','emptyDirsList','emptyDirCleanupResult','emptyDirTaskStatus'];
let pending=[],toasts=[],popups=[];
const nodes=new Map(ids.map(id=>[id,{value:'',textContent:'',innerHTML:'',disabled:false,style:{},classList:{contains:()=>true}}]));
const ctx=vm.createContext({$:id=>nodes.get(id),encodeURIComponent,openModal:html=>popups.push(html),closeModal:()=>{},
 esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 api:(url,opts)=>new Promise(resolve=>pending.push({url,opts,resolve})),toast:(...a)=>toasts.push(a),confirm:()=>true});
vm.runInContext(html.slice(html.indexOf('var __emptyDirsPreview = []'),html.indexOf('var __cloudResiduePreview = null')),ctx);
const roots=[{lib:'local',name:'本地库',path:'/media/local',available:true},{lib:'share',name:'分享库',path:'/media/share',available:true}];
const item={lib:'local',path:'/media/local/剧集/show {tmdb-1}',abs_path:'/media/local/剧集/show {tmdb-1}',rel:'剧集/show {tmdb-1}',cloud_path:'/media/cloud/剧集/show {tmdb-1}',file_count:20,nas_file_count:20,extensions:{'.ass':20},type:'附属文件残留'};
async function main(){
 assert(/id="emptyDirScope" value="all"/.test(html));
 assert(!html.match(/<button[^>]*id="emptyDirScanBtn"[^>]*>/)[0].includes('disabled'));
 assert(pending.length===0);
 console.log('PASS initial whole-library scope permits preview without an automatic scan');
 for(const scope of [{path:'',roots},{path:'/media/local/剧集',lib:'local',roots},{path:'/media/share',lib:'share',roots}]){
   ctx.__emptyDirBrowser.data=scope;ctx.useEmptyDirSelection(true);
   assert(pending.length===0 && !nodes.get('emptyDirScanBtn').disabled);
   assert(nodes.get('emptyDirTaskStatus').textContent.includes('范围已选择'));
 }
 console.log('PASS confirming entire/local/share scope only selects and enables manual preview');
 let task=ctx.scanEmptyDirs(),req=pending.shift();
 assert(nodes.get('emptyDirScanBtn').textContent==='扫描中…');
 req.resolve({status:'success',hits:1,preview:[item]});await task;
 assert(!nodes.get('emptyDirScanBtn').disabled && nodes.get('emptyDirTaskStatus').textContent.includes('扫描完成'));
 task=ctx.cleanEmptyDirs();req=pending.shift();
 assert(nodes.get('cleanEmptyBtn').textContent==='清理中…');
 await ctx.cleanEmptyDirs();assert(!pending.length);
 req.resolve({status:'success',nas_count:1,files_removed:20,cloud_count:1,cloud_files_removed:20,failed_count:0,errors:[],audit_recorded:true,completed_at:'2026-10-09 11:10:00'});await task;
 assert(popups.length===1 && popups.at(-1).includes('清理完成'));
 assert(popups.at(-1).includes('查看计划存档'));
 assert(!nodes.get('emptyDirsList').innerHTML.includes('20 个文件'));
 assert(!html.includes('id="emptyDirCleanupResult"'));
 assert(!pending.length && !nodes.get('emptyDirScanBtn').disabled && !ctx.__emptyDirsPreview.length);
 console.log('PASS one completion popup shows counts and records link; preview cleared without rescanning');
 ctx.__emptyDirsPreview=[item];task=ctx.cleanEmptyDirs();req=pending.shift();
 req.resolve({status:'success',cloud_files_removed:19,failed_count:1,errors:['<ass> denied','<season> not empty','<show> not empty'],audit_recorded:true});await task;
 assert(popups.at(-1).includes('未完成 1 项'));
 assert(popups.at(-1).includes('&lt;ass&gt; denied'));
 assert(!nodes.get('emptyDirsList').innerHTML.includes('denied'));
 assert(popups.at(-1).includes('有未完成项目'));
 console.log('PASS several failure messages for one directory count as one incomplete item');
 ctx.__emptyDirsPreview=[item];task=ctx.cleanEmptyDirs();req=pending.shift();
 req.resolve({status:'success',nas_count:1,files_removed:20,errors:[],audit_error:'record <denied>'});await task;
 assert(popups.at(-1).includes('清理完成'));
 assert(popups.at(-1).includes('record &lt;denied&gt;'));
 assert(!popups.at(-1).includes('查看计划存档'));
 console.log('PASS failed record write remains visible without hiding the successful deletion');
 console.log('PASS 5/5; DOM-stub behavior checks only');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
