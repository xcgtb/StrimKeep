// Real delete handlers with deferred HTTP responses; no production media touched.
const assert = require('assert'), vm = require('vm');
const source = require('./frontend_source.cjs').asset('/static/js/mapping.js');
function deferred(){ let resolve, reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; }
async function settle(){ for(let i=0;i<12;i++) await Promise.resolve(); }
function environment(api){
  const calls=[], alerts=[], toasts=[], patches=[], modals=[];
  const buttons=[{disabled:false,setAttribute(){}},{disabled:false,setAttribute(){}}];
  const status={hidden:true,textContent:''};
  const nodes={mappingDeleteStatus:status,mappingDeleteBar:{},mappingDeleteCounts:{textContent:''},mappingDeleteFile:{textContent:''}};
  const ctx={Promise,Array,String,Number,Math,Object,Set,Date,mappingPagedActive:false,
    document:{querySelectorAll:()=>buttons}, window:{__embySeriesMap:{}},esc:String,
    openModal:html=>modals.push(html),
    $:id=>nodes[id] || {textContent:''},
    hydratePosters(){},
    embyData:{series:[],movies:[{id:'m',tmdb_id:'7',in_local:true}],stats:{}},
    api:async(path,opts)=>{const body=opts && opts.body ? JSON.parse(opts.body) : {};calls.push({path,body});return api(path,body);},
    confirm:()=>{ctx.confirms++;return true;},alert:m=>alerts.push(m),
    toast:(message,type)=>toasts.push({message,type}),
    closeModal:()=>{ctx.closed++;},renderEmbyList(){},
    applySeriesDeleteResult:(id,r)=>patches.push({id,r}),
    setTimeout:fn=>{fn();},confirms:0,closed:0};
  vm.createContext(ctx);
  const start=source.indexOf('var mappingDeleteBusy =');
  if(start>=0) vm.runInContext(source.slice(start,source.indexOf('function showMovieDetailById(')),ctx);
  vm.runInContext(source.slice(source.indexOf('async function deleteMovieByTmdb('),source.indexOf('function showEmbyDetailById(')),ctx);
  vm.runInContext(source.slice(source.indexOf('async function deleteSeriesFromLib('),source.indexOf('/* 只重新拉取单部剧集')),ctx);
  return {ctx,calls,alerts,toasts,patches,buttons,status,modals,nodes};
}
const completed={status:'success',count:4,strm_removed:4,cloud_removed:4,errors:[],series_removed:true};
(async()=>{
  // First deletion is still pending. A second click must not preview/submit again.
  let gate=deferred();
  let e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:gate.promise);
  let first=e.ctx.deleteSeriesFromLib('s','local','剧');await settle();
  let second=e.ctx.deleteSeriesFromLib('s','local','剧');await settle();
  assert.equal(e.calls.length,2,'duplicate click submitted another deletion');
  assert.equal(e.ctx.confirms,1);assert(e.buttons.every(b=>b.disabled));
  assert(e.status.textContent.includes('正在删除'));assert.equal(e.status.hidden,false);
  assert.equal(e.modals.length,1);assert(e.status.textContent.includes('正在删除本地 STRM 及 115 源文件'));
  assert(e.modals[0].includes('待处理 4 个 STRM'));assert(e.modals[0].includes('删除继续执行'));
  // Reopening a modal cannot reset the in-flight state or start a movie deletion.
  e.buttons.forEach(b=>b.disabled=false);e.ctx.renderMappingDeleteState();
  assert(e.buttons.every(b=>b.disabled));
  await e.ctx.deleteMovieByTmdb('7','share','电影');assert.equal(e.calls.length,2);
  gate.resolve(completed);await Promise.all([first,second]);
  assert(e.buttons.every(b=>!b.disabled));assert.equal(e.status.hidden,true);
  assert.equal(e.toasts.filter(t=>t.type==='success').length,1);assert.equal(e.alerts.length,0);
  console.log('PASS pending deletion blocks repeated clicks, cross-type submissions and reopened modals');

  // The guard also covers the potentially slow Emby preview.
  gate=deferred();e=environment((_p,b)=>b.dry_run?gate.promise:completed);
  first=e.ctx.deleteSeriesFromLib('s','local','剧');await settle();
  second=e.ctx.deleteSeriesFromLib('s','local','剧');await settle();
  assert.equal(e.calls.length,1);assert(e.status.textContent.includes('核对'));
  gate.resolve({status:'success',count:4});await Promise.all([first,second]);
  assert.equal(e.calls.length,2);assert(!e.ctx.mappingDeleteBusy);
  console.log('PASS preview is protected before the first await');

  e=environment(()=>({status:'success',count:4}));e.ctx.confirm=()=>false;
  await e.ctx.deleteSeriesFromLib('s','share','剧');assert.equal(e.calls.length,1);
  assert.equal(e.modals.length,0);
  assert(e.buttons.every(b=>!b.disabled));assert.equal(e.toasts.length,0);
  e=environment(()=>{throw Error('preview unavailable');});
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert.equal(e.calls.length,1);assert(e.modals.at(-1).includes('未提交删除'));
  assert(!e.ctx.mappingDeleteBusy);
  console.log('PASS cancel and preview errors release the guard without claiming deletion');

  // Preview counts may become stale if another client finishes deleting.
  e=environment((p,b)=>p.endsWith('/resync')?{status:'success',series_removed:true}:
    b.dry_run?{status:'success',count:4}:{status:'success',count:0,ghost:true});
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert.equal(e.alerts.length,0);assert(!e.toasts.some(t=>t.type==='success'));
  assert(e.toasts.some(t=>t.message.includes('未新增删除')));
  assert.equal(e.calls.filter(c=>c.path.endsWith('/resync')).length,1);
  assert.equal(e.patches.length,1);
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,count:3,strm_removed:3,cloud_removed:3});
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert.equal(e.alerts.length,0);assert.equal(e.toasts.at(-1).type,'success');
  console.log('PASS no-op deletion has no green zero-success or empty warning; actual counts override stale previews');

  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,strm_removed:0,cloud_removed:0,series_removed:false,errors:['未找到云端源文件，STRM 保留']});
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert(!e.toasts.some(t=>t.type==='success'));assert(e.modals.at(-1).includes('未找到云端源文件'));
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,strm_removed:1,cloud_removed:1,series_removed:false,errors:['后续文件读取失败']});
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert(e.modals.at(-1).includes('1 个'));assert(e.modals.at(-1).includes('3 个'));
  assert(e.modals.at(-1).includes('后续文件读取失败'));assert(!e.toasts.some(t=>t.type==='success'));
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,strm_removed:0,cloud_removed:0,series_removed:false});
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert(e.modals.at(-1).includes('未返回具体原因'));assert(!e.modals.at(-1).includes('云端源文件找不到/匹配不唯一'));
  console.log('PASS zero/partial results report actual counts and real errors with a nonempty fallback');

  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:Promise.reject(Error('connection lost')));
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert(e.modals.at(-1).includes('结果未确认'));assert(!e.modals.at(-1).startsWith('删除失败'));
  assert.equal(e.calls.filter(c=>!c.path.includes('/progress/')).length,2);assert(!e.ctx.mappingDeleteBusy);
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:completed);
  e.ctx.applySeriesDeleteResult=()=>{throw Error('DOM update error');};
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert(e.modals.at(-1).includes('界面更新失败'));assert(e.modals.at(-1).includes('4 个'));
  assert(!e.modals.at(-1).startsWith('删除失败'));assert(!e.ctx.mappingDeleteBusy);
  console.log('PASS transport ambiguity and UI errors cannot erase confirmed deletion counts or auto-retry');

  for(const handler of ['series','movie']){
    e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:{status:'busy',message:'已有治理任务正在执行'});
    if(handler==='series')await e.ctx.deleteSeriesFromLib('s','local','剧');
    else await e.ctx.deleteMovieByTmdb('7','local','电影');
    assert.equal(e.alerts.length,0);assert(e.toasts.at(-1).message.includes('已有治理任务'));
    assert(!e.ctx.mappingDeleteBusy);
  }
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,strm_removed:1,cloud_removed:1,errors:['匹配不唯一']});
  await e.ctx.deleteMovieByTmdb('7','local','电影');assert.equal(e.ctx.embyData.movies.length,1);
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:completed);
  await e.ctx.deleteMovieByTmdb('7','local','电影');assert.equal(e.ctx.embyData.movies.length,0);
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:{...completed,cloud_removed:0});
  await e.ctx.deleteSeriesFromLib('s','share','剧');assert(!e.toasts.at(-1).message.includes('云端'));
  assert(e.modals[0].includes('仅删除分享库 STRM，保留云端源文件'));
  console.log('PASS backend busy responses, partial movie retention and share-only reporting');

  // Use the actual result patcher to verify both libraries remove finished cards.
  for(const target of ['local','share']){
    e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:completed);
    e.ctx.embyData.series=[{id:'s',in_local:target==='local',in_share:target==='share'}];
    e.ctx.window.__embySeriesMap.s=e.ctx.embyData.series[0];
    vm.runInContext(source.slice(source.indexOf('function applySeriesDeleteResult('),source.indexOf('async function refreshSingleSeriesInList(')),e.ctx);
    await e.ctx.deleteSeriesFromLib('s',target,'剧');
    assert.equal(e.ctx.embyData.series.length,0);assert.equal(e.ctx.window.__embySeriesMap.s,undefined);
    assert.equal(e.ctx.embyData.stats.total_series,0);
  }
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,series_removed:false,entry:{id:'s',in_local:false,in_share:true,local_eps:0,share_eps:2}});
  e.ctx.embyData.series=[{id:'s',in_local:true,in_share:true}];
  vm.runInContext(source.slice(source.indexOf('function applySeriesDeleteResult('),source.indexOf('async function refreshSingleSeriesInList(')),e.ctx);
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert.equal(e.ctx.embyData.series.length,1);assert.equal(e.ctx.embyData.series[0].in_local,false);
  assert.equal(e.ctx.embyData.series[0].share_eps,2);
  // A surviving library owns the new primary ID; details must use its live key.
  e=environment(()=>completed);
  e.ctx.embyData.series=[{id:'s',series_ids:['s','survivor'],in_local:true,in_share:true}];
  e.ctx.window.__embySeriesMap.s=e.ctx.embyData.series[0];
  vm.runInContext(source.slice(source.indexOf('function applySeriesDeleteResult('),source.indexOf('async function refreshSingleSeriesInList(')),e.ctx);
  e.ctx.applySeriesDeleteResult('survivor',{series_removed:false,entry:{id:'survivor',series_ids:['survivor'],in_local:false,in_share:true}});
  assert.equal(e.ctx.embyData.series[0].id,'survivor');
  assert.equal(e.ctx.window.__embySeriesMap.s,undefined);
  assert.equal(e.ctx.window.__embySeriesMap.survivor.id,'survivor');
  // Movie replies carry exact surviving identities, including partial deletions.
  e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:
    {...completed,strm_removed:3,errors:['one source retained'],movie_entries:[{id:'alive',ids:['alive'],tmdb_id:'7',in_local:true,in_share:true}]});
  e.ctx.embyData.movies=[{id:'dead',ids:['dead','alive'],tmdb_id:'7',in_local:true,in_share:true}];
  await e.ctx.deleteMovieByTmdb('7','local','电影');
  assert.equal(e.ctx.embyData.movies[0].id,'alive');
  assert.equal(e.ctx.window.__embyMovieMap.dead,undefined);
  assert.equal(e.ctx.window.__embyMovieMap.alive.id,'alive');
  console.log('PASS finished local/share series leave the grid; the other library retains its actual files');
  // Same-modal completion and both directions of two-library preservation.
  for(const target of ['local','share']) {
    e=environment((_p,b)=>b.dry_run?{status:'success',count:4}:completed);
    e.ctx.embyData.movies=[{id:'m',tmdb_id:'7',name:'双库电影',in_local:true,in_share:true}];
    await e.ctx.deleteMovieByTmdb('7',target,'双库电影');
    assert.equal(e.ctx.embyData.movies.length,1);
    assert.equal(e.ctx.embyData.movies[0][target==='local'?'in_local':'in_share'],false);
    assert.equal(e.ctx.embyData.movies[0][target==='local'?'in_share':'in_local'],true);
    assert(e.modals.at(-1).includes('删除完成'));
    assert(e.modals.at(-1).includes(target==='local'?'分享库仍有文件':'本地库仍有文件'));
    assert.equal(e.ctx.closed,0,'completion must remain in the same modal');
  }
  for (const target of ['local', 'share']) {
  let reads=0;
  e=environment((p,b)=>{
    if(b.dry_run)return {status:'success',count:4};
    if(p.includes('/progress/')){
      reads++;
      return reads===1 ? {status:'success',state:'running',phase:'deleting',total:4,processed:2,strm_removed:1,cloud_removed:1,retained:1,current_file:'E02.strm'}
        : {status:'success',state:'done',phase:'done',total:4,processed:4,strm_removed:4,cloud_removed:4,result:completed};
    }
    assert.equal(b.async_delete,true); assert(/^[a-f0-9]{32}$/.test(b.operation_id));
    return {status:'accepted',operation_id:b.operation_id};
  });
  let counts=[];let countText='';
  Object.defineProperty(e.nodes.mappingDeleteCounts,'textContent',{set:v=>{countText=v;counts.push(v);},get:()=>countText});
  await e.ctx.deleteSeriesFromLib('s',target,'剧');
  assert.equal(e.calls.filter(c=>c.body.dry_run===false).length,1);
  assert.equal(reads,2);assert(counts.some(x=>x.includes('已处理 2 / 4') && x.includes('保留 1')));
  assert(e.modals.at(-1).includes('删除完成'));assert.equal(e.alerts.length,0);assert.equal(e.ctx.closed,0);
  console.log('PASS real polled counts, single submission, same-modal results and both-library movie retention');
  if(target === 'share') assert(counts.every(x=>!x.includes('115 源文件')));
  }

  // Lost acceptance is recovered by reading its operation ID, never posting again.
  e=environment((p,b)=>{
    if(b.dry_run)return {status:'success',count:4};
    if(p.includes('/progress/'))return {status:'success',state:'done',result:completed};
    throw Error('acceptance connection lost');
  });
  await e.ctx.deleteSeriesFromLib('s','local','剧');
  assert.equal(e.calls.filter(c=>c.body.dry_run===false).length,1);
  assert(e.modals.at(-1).includes('删除完成'));
  console.log('PASS lost acceptance recovers the result by ID without duplicate deletion');
  console.log('PASS mapping deletion feedback regression suite');
})().catch(e=>{console.error(e);process.exitCode=1;});
