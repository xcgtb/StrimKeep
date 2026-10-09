// Minimal DOM-stub behavior checks, not browser visual testing.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const ids=['emptyDirBrowserBody','modalBg','emptyDirScope','emptyDirPath','emptyDirSelectedPath','emptyDirChooseBtn','emptyDirScanBtn','cleanEmptyBtn','emptyDirsStats','emptyDirsList','emptyDirCleanupResult'];
const nodes=new Map(ids.map(id=>[id,{value:'',textContent:'',innerHTML:'',disabled:true,style:{},classList:{contains:()=>true}}]));
let pending=[],toasts=[],confirmation='',popups=[];
const ctx=vm.createContext({$:id=>nodes.get(id),encodeURIComponent,openModal:html=>popups.push(html),closeModal:()=>{},
 esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 api:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve})),
 toast:(...a)=>toasts.push(a),confirm:s=>{confirmation=s;return true;}});
vm.runInContext(html.slice(html.indexOf('var __emptyDirsPreview = []'),html.indexOf('var __cloudResiduePreview = null')),ctx);
const item={path:'/media/local/剧集/Show {tmdb-1}',abs_path:'/media/local/剧集/Show {tmdb-1}',rel:'剧集/Show {tmdb-1}',lib:'local',local_present:true,cloud_present:null,cloud_checked:false,cloud_path:'/media/cloud/剧集/Show {tmdb-1}',cloud_file_count:null,nas_file_count:20,file_count:20,extensions:{'.ass':20},type:'附属文件残留',rule:'linked'};
const result={status:'success',preview:[item],hits:1,empty_dirs:1,folders:10,preview_limit:100};
async function main(){
 nodes.get('emptyDirPath').value='/media/local';nodes.get('emptyDirScope').value='directory';
 let task=ctx.scanEmptyDirs(),req=pending.shift();req.resolve(result);await task;
 let rendered=nodes.get('emptyDirsList').innerHTML;
 assert(rendered.includes('执行时通过 CD2 核对并清理')&&rendered.includes(item.cloud_path)&&rendered.includes('115 删除数量在执行后显示'));
 assert(!rendered.includes('预览 115 残留'));
 console.log('PASS NAS preview shows CD2 mapping without claiming cloud was scanned');
 ctx.confirm=()=>false;await ctx.cleanEmptyDirs();assert(pending.length===0);ctx.confirm=s=>{confirmation=s;return true;};
 console.log('PASS cancellation performs no cleanup');
 task=ctx.cleanEmptyDirs();req=pending.shift();assert(req.url.endsWith('/clean'));
 assert.deepStrictEqual(JSON.parse(req.options.body),{items:[{path:item.path,lib:'local',cloud_path:item.cloud_path}]});
 assert(confirmation.includes('NAS 和对应 115'));
 req.resolve({status:'success',nas_count:0,cloud_count:1,files_removed:0,cloud_files_removed:20,errors:[]});await task;
 assert(popups.at(-1).includes('115：1 个目录、20 个文件'));
 assert(popups.at(-1).includes('清理完成'));
 assert(popups.at(-1).includes('查看计划存档'));
 assert(!nodes.get('emptyDirsList').innerHTML.includes('115：1 个目录'));
 assert(pending.length===0 && ctx.__emptyDirsPreview.length===0);
 assert(nodes.get('emptyDirsList').innerHTML.includes('预览扫描'));
 console.log('PASS one clean request includes cloud mapping and persists separate NAS/115 counts');
 ctx.__emptyDirsPreview=[item];task=ctx.cleanEmptyDirs();req=pending.shift();
 req.resolve({status:'success',nas_count:0,cloud_count:0,files_removed:0,cloud_files_removed:19,errors:['/media/cloud/<failed>.ass: denied']});await task;
 rendered=popups.at(-1);
 assert(rendered.includes('19 个文件')&&rendered.includes('&lt;failed&gt;.ass: denied'));
 assert(ctx.__emptyDirsPreview.length===0);
 console.log('PASS partial cloud cleanup reports deleted count and failed path/reason');
 ctx.__emptyDirsPreview=[item];task=ctx.cleanEmptyDirs();req=pending.shift();
 ctx.clearEmptyDirsPreview();ctx.__emptyDirsPreview=[{path:'/media/share/new',lib:'share'}];
 req.resolve({status:'success',nas_count:0,cloud_count:1,cloud_files_removed:20,errors:[]});await task;
 assert(ctx.__emptyDirsPreview[0].lib==='share'&&pending.length===0);
 console.log('PASS late cleanup response cannot erase a newer selection');
 console.log('PASS 5/5; DOM-stub behavior checks only');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
