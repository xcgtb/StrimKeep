/* Media overview: cache-first, one refresh action, isolated recommendations. */
var overviewControl={timer:null,promise:null,refreshId:0,noticeId:0,lastAt:0};
var overviewData=null,libStatMode='all';
var overviewHot={media:'movie',timer:null,promise:null,generation:0,rows:{movie:[],tv:[]},signature:'',lastAt:0};
function overviewActive(){return !document.hidden && window.__activeTab==='dashboard';}
function overviewStore(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch(e){}}
function overviewCached(key){try{return JSON.parse(localStorage.getItem(key)||'null');}catch(e){return null;}}
function saveDashboardCache(d){
  try { var cached = Object.assign({},d); delete cached.storage; localStorage.setItem('dashboardCache', JSON.stringify(cached)); } catch(e){}
}
function saveLibStatsCache(r){overviewStore('libStatsCache',r);}
function hydrateDashboardFromCache(){
  var saved=overviewCached('overviewCache');
  if(saved&&saved.dashboard){overviewData=saved;renderOverview(saved);}
  else{
    var d=overviewCached('dashboardCache'),lib=overviewCached('libStatsCache'),health=overviewCached('dashEmbyCache');
    if(d)renderDashboard(d);if(lib)renderLibraryStats(lib);if(health)renderDashEmby(health);
  }
  var rows=overviewCached('overviewHot-'+overviewHot.media);if(Array.isArray(rows)&&rows.length){overviewHot.rows[overviewHot.media]=rows;renderOverviewRecommendations(rows);}
}
function setLibStatMode(m){
  libStatMode=m;
  document.querySelectorAll('#libStatTabs button').forEach(function(b){b.classList.toggle('active',b.dataset.m===m);});
  if(window.__libStats)renderLibraryStats(window.__libStats);
  // Completeness is an identity-union fact, not a count of duplicated files.
}
function renderLibraryStats(r){
  window.__libStats=r;
  var m=libStatMode,rows=(r.rows||[]).map(function(x){return{name:x.name,n:Number(m==='local'?x.local:m==='share'?x.share:x.total)||0};}).filter(function(x){return x.n>0;}).sort(function(a,b){return b.n-a.n;});
  var max=rows.length?rows[0].n:1;
  $('libStatsGrid').innerHTML=rows.map(function(row,i){
    return '<button type="button" class="cinema-category" data-name="'+esc(row.name)+'" data-scope="'+m+'" onclick="dashOpenCat(this.dataset.name,this.dataset.scope)"><span class="cinema-category-dot tone-'+(i%5)+'"></span><span class="nm">'+esc(row.name)+'</span><span class="tr" aria-hidden="true"><i class="tone-'+(i%5)+'" style="width:'+(row.n/max*100)+'%"></i></span><b>'+row.n.toLocaleString()+'</b></button>';
  }).join('')||'<div class="list-empty">暂无分类数据</div>';
  var l=Number(r.local_total)||0,s=Number(r.share_total)||0,other=Number(m==='local'?r.local_other:m==='share'?r.share_other:(Number(r.local_other)||0)+(Number(r.share_other)||0))||0;
  $('libStatsSummary').textContent=(m==='local'?'本地库':m==='share'?'分享库':'两库合计')+' '+(m==='local'?l:m==='share'?s:l+s).toLocaleString()+' 个 STRM'+(other?' · 未分类 '+other.toLocaleString():'');
}
async function loadLibraryStats(){if(overviewData&&overviewData.library_stats)renderLibraryStats(overviewData.library_stats);else return loadDashboard();}
function fmtBig(n){return(n||0).toLocaleString();}
function fmtUnit(n,u){return(n||0).toLocaleString()+'<i class="u">'+u+'</i>';}
function renderDashEmby(x){
  var factsTs=x.facts_ts||x.ts||0;
  var stale=x.stale||(x.ts&&Date.now()/1000-x.ts>1800);
  $('dash-facts-summary').textContent=stale?'对照缓存已过期':'数据更新时间';
  $('dash-library-facts').textContent=factsTs?'集数更新 '+new Date(factsTs*1000).toLocaleString('zh-CN')+(x.ts?' · TMDB 对照 '+new Date(x.ts*1000).toLocaleString('zh-CN')+(stale?'（已过期）':''):' · 尚无完整 TMDB 对照'):'暂无片库对照缓存';
  $('dash-series').innerHTML=fmtUnit(x.series,'部');$('dash-movies').innerHTML=fmtUnit(x.movies,'部');$('dash-eps').innerHTML=fmtBig(x.eps);
  var st=x.st||{},rows=[['完整','ok',st.aligned],['缺集','err',st.missing],['超集','warn',st.extra],['在更','info',st.ongoing],['未匹配','dim',(st.unmatched||0)+(st.no_tmdb||0)]];
  var sum=rows.reduce(function(n,r){return n+(Number(r[2])||0);},0);
  $('dash-health-bar').innerHTML=rows.map(function(r){return '<i class="'+r[1]+'" style="flex-grow:'+(Number(r[2])||0)+'"></i>';}).join('');
  $('dash-health-bar').classList.toggle('empty',!sum);
  $('dash-health').innerHTML=rows.map(function(r){return '<button type="button" data-filter="'+({ok:'aligned',err:'missing',warn:'extra',info:'ongoing',dim:'unmatched'}[r[1]])+'" onclick="overviewOpenHealth(this.dataset.filter)"><i class="dd '+r[1]+'"></i><span>'+r[0]+'</span><b>'+(Number(r[2])||0).toLocaleString()+'</b></button>';}).join('');
}
async function loadDashEmby(){
  // Existing mapping updates call this hook; reuse the shared health facts.
  try{var r=await api('/api/library/health',{timeoutMs:8000});if(r.status!=='success')return;
    var st=r.stats||{},sum={series:st.total_series||((r.series||[]).length),movies:st.total_movies||((r.movies||[]).length),eps:r.episodes!=null?r.episodes:0,st:st,ts:r.ts,facts_ts:r.facts_ts,facts_version:r.facts_version,stale:r.stale};
    renderDashEmby(sum);overviewStore('dashEmbyCache',sum);
  }catch(e){if(typeof embyLoaded!=='undefined'&&embyLoaded){var rows=embyData.series||[];renderDashEmby({series:rows.length,movies:(embyData.movies||[]).length,eps:rows.reduce(function(n,s){return n+(s.have_eps!=null?s.have_eps:(s.total_episodes||0));},0),st:embyData.stats||{}});}}
}
function overviewOpenHealth(filter){switchTab('mapping');setEmbyType('series');setEmbyScope('all');setEmbyFilter(filter);}
function renderDashPlanFromDashboard(d){
  var x=d.lastScan;
  $('overviewGovRecent').innerHTML=x?'<span class="cinema-recent-icon">'+icon('check')+'</span><div><b>双库扫描完成</b><span>本地待处理 '+(x.local||0)+' 项 · 分享待处理 '+(x.share||0)+' 项 · 受保护 '+(x.protected||0)+' 项</span></div><time>'+esc(x.time||'')+'</time>':'<span class="muted">暂无治理扫描 · 前往双库治理生成清单</span>';
  var rc=x&&x.reason_counts||{},labels={share_better:'分享画质更优',local_better:'本地画质更优',share_wins:'分享择优',local_wins:'本地择优',whitelist:'白名单豁免',protected:'受保护',exempt:'白名单豁免',decision_keep_local:'保留本地',decision_keep_share:'保留分享'};
  var rows=Object.keys(rc).filter(function(k){return rc[k]>0;}).sort(function(a,b){return rc[b]-rc[a];}).slice(0,8);
  $('dash-gov-reason-note').textContent=x?'扫描于 '+fmtGovAgo(x.age_sec||0):'';
  $('dash-gov-reasons').innerHTML=rows.map(function(k){return '<div class="overview-reason"><span>'+esc(labels[k]||k)+'</span><b>'+rc[k]+'</b></div>';}).join('')||'<div class="list-empty">暂无治理决策</div>';
}
function renderOverviewStrategy(s){
  if(!s)return;
  var decision={quality_first:'画质优先',keep_local:'保留本地',keep_share:'保留分享',balanced:'严格画质'}[s.decision]||s.decision||'画质优先';
  var multi=s.multi_season_protect==='off'?'多季保护关闭':'多季保护';
  var special={keep:'保留特别篇',ignore:'忽略特别篇',delete:'清理特别篇'}[s.special_action]||'保留特别篇';
  $('strategySnapshot').innerHTML='<div class="cinema-strategy-tags"><span class="primary">'+esc(decision)+'</span><span>'+multi+'</span><span>'+special+'</span></div><p>平局'+(s.tie_keep_local?'保留本地':'保留分享')+' · 达标率 <b>'+Math.round(Number(s.season_replace_ratio==null?0.9:s.season_replace_ratio)*100)+'%</b></p>'+(s.exempt_keywords&&s.exempt_keywords.length?'<small>白名单：'+s.exempt_keywords.map(esc).join('、')+'</small>':'');
}
function renderStorageStatus(status){
  var panel = $('dash-storage-warning'), list = $('dash-storage-issues');
  if (!panel || !list) return;
  var issues = status && status.issues || [];
  panel.style.display = issues.length ? '' : 'none';
  var labels = {
    db_save_plan:'计划数据库写入', db_load_plan:'计划数据库读取', db_save_plan_state:'计划状态保存',
    db_list_plans:'计划列表读取', db_purge_plans:'过期计划处理', db_sync_plans_from_disk:'计划索引同步',
    db_media_index_load:'媒体索引读取', db_media_index_upsert:'媒体索引保存', db_media_index_delete_missing:'媒体索引更新',
    db_add_audit:'执行记录保存', db_recent_audit:'执行记录读取', db_get_audit:'记录详情读取',
    db_load_sub_state:'订阅状态数据库读取', db_save_sub_state:'订阅状态数据库写入', db_clear_sub_state:'订阅重置',
    db_doc_set:'状态保存', db_doc_get:'状态读取', db_doc_mirror_file:'旧状态导入',
    state_write:'SQLite 状态保存', state_read:'SQLite 状态读取', state_migration:'SQLite 数据迁移',
    runtime_logs_write:'实时日志保存', runtime_logs_read:'实时日志读取',
    db_kv_get:'运行状态读取', db_kv_set:'运行状态保存', db_dedup_add:'消息去重状态保存', db_dedup_seen:'消息去重状态读取',
    subscriptions_state_read:'订阅状态文件读取', subscriptions_state_write:'订阅状态文件保存',
    plan_retention:'过期计划处理', plan_file_write:'计划文件保存', latest_scan_write:'扫描结果保存', audit_export_write:'执行记录文件保存',
    state_temporary_cleanup:'临时状态文件处理'
  };
  list.innerHTML = issues.map(function(issue){
    var when = issue.last_at ? new Date(issue.last_at * 1000).toLocaleString() : '';
    return '<div style="margin:8px 0;line-height:1.6"><b>' + esc(labels[String(issue.operation).split(':')[0]] || '运行数据读写')
      + '</b>：' + esc(issue.message || '读写失败') + (when ? '<div class="muted">最近发生 ' + esc(when) + '</div>' : '') + '</div>';
  }).join('');
}
async function loadStorageStatus(){
  try { var r = await api('/api/storage/status',{timeoutMs:5000}); if (r.status === 'success') renderStorageStatus(r.storage); } catch(e){}
}
function renderDashboard(d){
  renderSidebarServices(d.services); if (d.storage) renderStorageStatus(d.storage);
  $('stat-local').textContent=d.localCount==null?'—':d.localCount;$('stat-share').textContent=d.shareCount==null?'—':d.shareCount;
  if(d.version){if($('sideVersion'))$('sideVersion').textContent=d.version;if($('headerVersion'))$('headerVersion').textContent=d.version;}
  if(d.services){var e=d.services.emby||{},t=d.services.tmdb||{};$('svc-emby').textContent=e.ok?'在线':'离线';$('overviewEmbyDot').classList.toggle('ok',!!e.ok);$('svc-tmdb').textContent=t.ok?'已配置':'未配置';if(e.host)window.__embyHost=e.host;}
  if(d.libraryHealth){var h=d.libraryHealth,st=h.stats||{},sum={series:st.total_series||0,movies:st.total_movies||0,eps:h.episodes||0,st:st,ts:h.ts,facts_ts:h.facts_ts,facts_version:h.facts_version,stale:h.stale};renderDashEmby(sum);overviewStore('dashEmbyCache',sum);}
  if(d.subscriptions){var ss=d.subscriptions;$('stat-sub').textContent=(ss.enabled||0)+' / '+(ss.total||0)+' 部';$('stat-sub-sub').textContent={running:'检查中',error:'检查失败',partial:'部分失败',disabled:'已停用',success:'最近检查正常'}[(ss.check||{}).status]||'点击查看';}
  if(d.morningReport){var mr=d.morningReport;$('stat-morning').textContent=mr.enabled?mr.time:'未开启';$('stat-morning-sub').textContent=mr.last_date?'上次 '+mr.last_date:'预扫 '+(mr.prescan_min||5)+' 分钟';}
  if(d.ingest){var ing=d.ingest;$('stat-ingest-mov').textContent=(ing.movies||0)+' / '+(ing.series||0)+' 部';$('stat-ingest-age').textContent=ing.cache_age_sec==null?'暂无缓存':Math.floor(ing.cache_age_sec/60)+' 分钟前 · 电影 / 剧集';}
  renderDashPlanFromDashboard(d);
}
function renderOverview(r){
  if(r.dashboard)renderDashboard(r.dashboard);if(r.library_stats&&r.library_stats.rows)renderLibraryStats(r.library_stats);renderOverviewStrategy(r.strategy);
  $('overviewUpdated').textContent=r.ts?'更新于 '+new Date(r.ts*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'等待首次更新';
  var job=r.refresh||{},btn=$('overviewRefresh');btn.disabled=!!job.running;btn.innerHTML=(job.running?'<span class="spin"></span>':icon('refresh'))+(job.running?'正在刷新':'刷新总览');
  var message=job.running?(job.phase||'正在刷新总览')+' · 完成后自动更新':job.error||((job.warnings||[]).join('；'))||'';
  $('overviewRefreshState').textContent=message;$('overviewRefreshState').classList.toggle('warning',!!job.error||(job.warnings||[]).length>0);
  if(overviewControl.refreshId&&job.id>=overviewControl.refreshId&&!job.running){
    if(overviewControl.noticeId!==job.id){overviewControl.noticeId=job.id;toast(job.error||((job.warnings||[]).length?'总览已更新，部分服务未能刷新':'总览已更新为最新数据'),job.error?'error':(job.warnings||[]).length?'':'success');}
    overviewControl.refreshId=0;
  }
}
function scheduleOverview(ms){clearTimeout(overviewControl.timer);if(overviewActive())overviewControl.timer=setTimeout(function(){loadDashboard(true);},ms);}
async function loadDashboard(recheck){
  if(overviewControl.promise)return overviewControl.promise;
  if(!recheck&&overviewData&&Date.now()-overviewControl.lastAt<10000){renderOverview(overviewData);scheduleOverview(overviewData.refresh&&overviewData.refresh.running?1500:30000);loadOverviewRecommendations();return;}
  overviewControl.promise=(async function(){
    try{var r=await api('/api/overview',{timeoutMs:8000});if(r.status==='error')throw new Error(r.message||'读取失败');overviewData=r;overviewControl.lastAt=Date.now();renderOverview(r);if(r.dashboard)overviewStore('overviewCache',r);scheduleOverview(r.refresh&&r.refresh.running?1500:30000);}
    catch(e){$('overviewRefreshState').textContent='连接失败，保留缓存，稍后自动重试';scheduleOverview(15000);}
    finally{overviewControl.promise=null;}
  })();
  loadStorageStatus();loadOverviewRecommendations();return overviewControl.promise;
}
async function refreshOverview(){
  if($('overviewRefresh').disabled)return;
  $('overviewRefresh').disabled=true;$('overviewRefreshState').textContent='正在清理总览缓存并获取最新数据…';
  try{var r=await api('/api/overview/refresh',{method:'POST',timeoutMs:8000});if(r.status!=='success')throw new Error(r.message||'刷新失败');overviewControl.refreshId=r.refresh.id;['overviewCache','dashboardCache','libStatsCache','dashEmbyCache'].forEach(function(k){try{localStorage.removeItem(k);}catch(e){}});if(overviewData){overviewData.refresh=r.refresh;renderOverview(overviewData);}await loadDashboard(true);scheduleOverview(1500);}
  catch(e){$('overviewRefresh').disabled=false;$('overviewRefreshState').textContent='刷新未能启动，请重试';toast(e.message,'error');}
}
function pauseOverview(){clearTimeout(overviewControl.timer);clearTimeout(overviewHot.timer);overviewHot.generation++;}
function setOverviewMedia(media){if(media===overviewHot.media)return;overviewHot.media=media;overviewHot.generation++;overviewHot.signature='';overviewHot.lastAt=0;clearTimeout(overviewHot.timer);document.querySelectorAll('#overviewHotTabs button').forEach(function(b){b.classList.toggle('active',b.dataset.media===media);});renderOverviewRecommendations(overviewHot.rows[media]||overviewCached('overviewHot-'+media)||[]);$('overviewHotShelf').scrollLeft=0;loadOverviewRecommendations(true);}
function scrollOverviewShelf(dir){var el=$('overviewHotShelf');el.scrollBy({left:dir*el.clientWidth*0.85,behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'});}
function overviewMoreRecommendations(){
  exploreState.media=overviewHot.media;exploreState.q='';exploreState.page=1;
  $('exploreSearch').value='';
  document.querySelectorAll('#filterMedia .chip').forEach(function(b){b.classList.toggle('active',b.dataset.v===overviewHot.media);});
  refreshGenreChipsForMedia(overviewHot.media);updateFilterSummaryText();
  switchTab('explore');
}
function renderOverviewRecommendations(rows){
  overviewHot.rows[overviewHot.media]=rows;
  var signature=JSON.stringify(rows);if(signature===overviewHot.signature)return;overviewHot.signature=signature;
  var shelf=$('overviewHotShelf'),left=shelf.scrollLeft;
  shelf.innerHTML=rows.length?rows.map(function(c,i){var known=c.library_status==='available',badge=c.in_emby?'已在库':known?'未入库':'待同步';
    return '<button type="button" class="cinema-film" onclick="openOverviewRecommendation('+i+')" aria-label="'+esc(c.title)+'"><span class="poster-wrap">'+(c.poster?'<span class="poster-loading">海报加载中</span><img data-emby-src="'+esc(c.poster)+'" alt="'+esc(c.title)+'" loading="lazy" decoding="async" onload="clearPosterLoading(this)">':'<span class="no-img">暂无海报</span>')+'<span class="cinema-rating">★ '+(Number(c.rating)||0).toFixed(1)+'</span><span class="cinema-inlibrary '+(c.in_emby?'in':'')+'">'+badge+'</span></span><b>'+esc(c.title)+'</b><small>'+esc(c.year||'年份未知')+' · '+(c.type==='tv'?'剧集':'电影')+'</small></button>';
  }).join(''):'<div class="cinema-recommendation-empty">暂无热门推荐</div>';
  shelf.scrollLeft=left;hydratePosters(shelf);
}
async function loadOverviewRecommendations(retry){
  if(!overviewActive())return;
  if(overviewHot.promise){overviewHot.promise.then(function(){if(overviewActive()&&overviewHot.lastAt===0)loadOverviewRecommendations(retry);});return;}
  if(!retry&&Date.now()-overviewHot.lastAt<30000)return;
  var generation=overviewHot.generation,media=overviewHot.media;
  overviewHot.promise=(async function(){
    var delay=60000;
    try{var r=await api('/api/overview/recommendations?media='+media+(retry?'&retry=1':''),{timeoutMs:8000});if(generation!==overviewHot.generation||!overviewActive())return;
      overviewHot.lastAt=Date.now();
      if(r.status==='success'){renderOverviewRecommendations(r.cards||[]);overviewStore('overviewHot-'+media,r.cards||[]);$('overviewHotNote').textContent=r.refresh_error?'推荐更新失败，展示缓存，稍后重试':'TMDB 本周热度 · '+(r.page_ts?new Date(r.page_ts*1000).toLocaleDateString('zh-CN'):'');if(r.refreshing)delay=2000;}
      else if(r.status==='pending'){if(!overviewHot.rows[media].length)$('overviewHotShelf').innerHTML='<div class="cinema-recommendation-empty">正在获取本周热门…</div>';delay=2000;}
      else{$('overviewHotNote').innerHTML=esc(r.message||'推荐暂不可用')+' <button type="button" onclick="loadOverviewRecommendations(true)">重试</button>';delay=15000;}
    }catch(e){if(generation===overviewHot.generation)$('overviewHotNote').innerHTML='推荐暂不可用，不影响片库统计 <button type="button" onclick="loadOverviewRecommendations(true)">重试</button>';delay=15000;}
    finally{overviewHot.promise=null;if(generation===overviewHot.generation&&overviewActive()){clearTimeout(overviewHot.timer);overviewHot.timer=setTimeout(function(){overviewHot.lastAt=0;loadOverviewRecommendations();},delay);}}
  })();return overviewHot.promise;
}
function openOverviewRecommendation(i){
  var c=overviewHot.rows[overviewHot.media][i];if(!c)return;
  var canSub=c.type==='tv',subs=typeof _subscribedTmdbIds==='function'?_subscribedTmdbIds():{};
  openModal('<div class="cinema-film-detail"><div class="mapping-detail-header"><div class="mapping-detail-poster">'+(c.poster?'<img data-emby-src="'+esc(c.poster)+'" alt="" onload="clearPosterLoading(this)">':'暂无海报')+'</div><div class="mapping-detail-info"><h3>'+esc(c.title)+'</h3><p>'+esc(c.year||'年份未知')+' · '+(canSub?'剧集':'电影')+' · ★ '+Number(c.rating||0).toFixed(1)+'</p><p>'+(c.in_emby?'已在库':c.library_status==='available'?'未入库':'片库状态待同步')+'</p></div></div><div class="cinema-detail-actions">'+(canSub?'<button type="button" class="sub-btn '+(subs[c.tmdb_id]?'on':'')+'" data-tmdb="'+esc(c.tmdb_id)+'" data-title="'+esc(c.title)+'" data-poster="'+esc(c.poster||'')+'">'+(subs[c.tmdb_id]?'✓ 已订阅':'+ 订阅追更')+'</button>':'')+'<a class="btn gray" href="https://www.themoviedb.org/'+(canSub?'tv/':'movie/')+encodeURIComponent(c.tmdb_id)+'" target="_blank" rel="noopener noreferrer">查看影视详情 ↗</a></div></div>');hydratePosters($('modalBody'));
}
document.addEventListener('visibilitychange',function(){if(document.hidden)pauseOverview();else if(overviewActive()){overviewHot.lastAt=0;loadDashboard(true);}});

/* Lightweight live status; this endpoint never starts a scan or remote check. */
var runtimeStatusControl = {timer:null,busy:false};
function setRuntimeLabel(id,text,tone){
  var el=$(id);if(!el)return;
  el.textContent=text;
  if(el.setAttribute)el.setAttribute('data-tone',tone||'unknown');
}
function renderSidebarServices(services){
  if(!services)return;
  var e=services.emby||{},t=services.tmdb||{};
  setRuntimeLabel('sideServices',(e.ok?'在线':'离线')+' / '+(t.ok?'已配置':'未配置'),e.ok&&t.ok?'ok':'warn');
}
function renderRuntimeStatus(r){
  var b=r.bot||{},state=b.state||(b.running?'ok':'stopped');
  var botLabels={ok:'轮询正常',starting:'启动中',degraded:'连接重试中',down:'无响应',stopped:'未启动'};
  setRuntimeLabel('sideConnection','已连接','ok');
  setRuntimeLabel('headerRuntime','已连接','ok');
  setRuntimeLabel('sideBot',botLabels[state]||'状态未知',state==='ok'?'ok':state==='down'?'bad':state==='stopped'?'unknown':'warn');
  var issues=(r.storage||{}).issues||[];
  setRuntimeLabel('sideStorage',issues.length?'有 '+issues.length+' 项异常':'未发现异常',issues.length?'bad':'ok');
  var task=r.task,p=r.comparison||{},names={inter_check:'双库扫描',inter_clean:'治理清理',empty_dirs_scan:'目录扫描',empty_dirs_clean:'目录清理',clean_orphans:'目录清理'};
  var taskText=task?(task.cancel_requested?'正在取消':names[task.kind]||'任务执行中'):(p.running?'片库对照 '+(Number(p.percent)||0)+'%':'空闲');
  setRuntimeLabel('sideTask',taskText,task||p.running?'running':'unknown');
  var ts=$('sideStatusTime');if(ts)ts.textContent='更新于 '+new Date((r.ts||Date.now()/1000)*1000).toLocaleTimeString();
  if(r.version){if($('sideVersion'))$('sideVersion').textContent=r.version;if($('headerVersion'))$('headerVersion').textContent=r.version;}
}
async function pollRuntimeStatus(){
  if(runtimeStatusControl.busy||document.hidden)return;
  runtimeStatusControl.busy=true;
  try{renderRuntimeStatus(await api('/api/runtime/status',{timeoutMs:5000}));}
  catch(e){
    setRuntimeLabel('sideConnection','连接中断','bad');setRuntimeLabel('headerRuntime','连接中断','bad');
    ['sideBot','sideStorage','sideTask'].forEach(function(id){setRuntimeLabel(id,'状态待更新','unknown');});
    if($('sideStatusTime'))$('sideStatusTime').textContent='连接失败，稍后自动重试';
  }finally{
    runtimeStatusControl.busy=false;
    clearTimeout(runtimeStatusControl.timer);
    if(!document.hidden)runtimeStatusControl.timer=setTimeout(pollRuntimeStatus,15000);
  }
}
function startRuntimeStatus(){clearTimeout(runtimeStatusControl.timer);pollRuntimeStatus();}
document.addEventListener('visibilitychange',function(){
  clearTimeout(runtimeStatusControl.timer);
  if(!document.hidden)startRuntimeStatus();
});
// Status lives in the header, outside the navigation drawer. Keep it compact
// and dismissible without opening/closing the media-detail modal.
document.addEventListener('click',function(event){
  var panel=$('runtimePopover');
  if(panel&&panel.open&&!panel.contains(event.target))panel.open=false;
});
document.addEventListener('keydown',function(event){
  var panel=$('runtimePopover');
  if(event.key==='Escape'&&panel&&panel.open){
    panel.open=false;
    var trigger=$('headerRuntime');if(trigger&&trigger.focus)trigger.focus();
  }
});
