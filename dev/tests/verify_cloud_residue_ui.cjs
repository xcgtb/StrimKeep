// Minimal DOM-stub checks, not a browser visual test.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=require('./frontend_source.cjs').loadFrontend();
const start=html.indexOf('var __cloudResiduePreview = null'),end=html.indexOf('/* ═══════════ 初始化 ═══════════ */',start);
const nodes=new Map(['cloudResidueBody','modalBg','cleanCloudResidueBtn'].map(id=>[id,{innerHTML:'',disabled:false,classList:{contains:()=>true}}]));
let pending=[],toasts=[];
const ctx=vm.createContext({
 $:id=>nodes.get(id),openModal:()=>{},closeModal:()=>{},confirm:()=>true,
 esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 emptyDirFileSummary:r=>'附属文件 '+r.file_count+' 个',
 __emptyDirsPreview:[{path:'/media/local/剧集/片名 {tmdb-1}'}],
 api:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve})),
 toast:(...args)=>toasts.push(args)
});
vm.runInContext(html.slice(start,end),ctx);
const preview={status:'success',token:'server-token',local_path:ctx.__emptyDirsPreview[0].path,
 cloud_path:'/media/cloud/剧集/片名 {tmdb-1}',file_count:20,files:['Season 1/<subtitle>.ass']};
async function open(result=preview){
 const task=ctx.previewCloudResidue(0);
 const req=pending.shift();assert(req.url.endsWith('/preview'));
 assert(JSON.parse(req.options.body).local_path===preview.local_path);
 req.resolve(result);await task;
}
async function main(){
 await open();
 let rendered=nodes.get('cloudResidueBody').innerHTML;
 assert(rendered.includes('&lt;subtitle&gt;.ass')&&rendered.includes('清理此 115 残留'));
 assert(rendered.includes('不产生备份')&&!rendered.includes('备份并清理'));
 console.log('PASS read-only preview escapes names and states direct deletion');
 ctx.confirm=()=>false;await ctx.cleanCloudResidue();assert(pending.length===0);
 ctx.confirm=()=>true;
 console.log('PASS cancellation sends no deletion request');
 let task=ctx.cleanCloudResidue(),req=pending.shift();
 assert(JSON.parse(req.options.body).token==='server-token'&&JSON.parse(req.options.body).confirmed===true);
 req.resolve({status:'partial',files_removed:19,errors:['<failed>.ass: denied']});await task;
 rendered=nodes.get('cloudResidueBody').innerHTML;
 assert(rendered.includes('已删除 19')&&rendered.includes('&lt;failed&gt;.ass: denied'));
 assert(ctx.__cloudResiduePreview===null);
 console.log('PASS partial cleanup shows count, failed paths and reasons');
 await open();
 const oldClean=ctx.cleanCloudResidue(),oldReq=pending.shift();
 await open({...preview,token:'new-token'});
 oldReq.resolve({status:'success',files_removed:20});await oldClean;
 assert(ctx.__cloudResiduePreview.token==='new-token');
 console.log('PASS old cleanup response cannot erase newer preview');
 task=ctx.cleanCloudResidue();req=pending.shift();
 req.resolve({status:'success',files_removed:20});await task;
 assert(nodes.get('cloudResidueBody').innerHTML.includes('20 个附属文件，无备份'));
 assert(ctx.__emptyDirsPreview.length===1&&ctx.__emptyDirsPreview[0].path===preview.local_path);
 console.log('PASS cloud success reports deletion while NAS preview remains');
 console.log('PASS 5/5; DOM-stub interaction checks only');
}
main().catch(e=>{console.error(e);process.exitCode=1;});

