/* ═══════════ 治理总览 ═══════════ */
/* 后端 /api/dashboard、/api/library_stats 本身已经带缓存了（5分钟/30秒 TTL），
   但前端每次打开页面都是先显示"-"/"检查中..."占位符，等接口返回才填真实值，
   视觉上像是"每次都在重新刷新"。这里加一层本地缓存，页面一打开就先用上次的
   数据瞬间填满（不再有占位符闪烁），网络请求仍然照常发，回来后静默更新即可。 */
function saveDashboardCache(d){
  try { var cached = Object.assign({},d); delete cached.storage; localStorage.setItem('dashboardCache', JSON.stringify(cached)); } catch(e){}
}
function saveLibStatsCache(r){
  try { localStorage.setItem('libStatsCache', JSON.stringify(r)); } catch(e){}
}
function hydrateDashboardFromCache(){
  try { var raw3 = localStorage.getItem('dashEmbyCache'); if (raw3) renderDashEmby(JSON.parse(raw3)); } catch(e){}
  try {
    var raw = localStorage.getItem('dashboardCache');
    if (raw) renderDashboard(JSON.parse(raw));
  } catch(e){}
  try {
    var raw2 = localStorage.getItem('libStatsCache');
    if (raw2) renderLibraryStats(JSON.parse(raw2));
  } catch(e){}
}

async function loadLibraryStats(){
  try {
    var r = await api('/api/library_stats');
    if (r.status !== 'success') throw new Error(r.message || '\u5931\u8d25');
    renderLibraryStats(r);
    saveLibStatsCache(r);
  } catch(e){
    // 有缓存的话保留缓存显示的内容，不要用错误信息把已经填好的数据覆盖掉
    if (!localStorage.getItem('libStatsCache')) {
      $('libStatsGrid').innerHTML = '<div class="list-empty">\u52a0\u8f7d\u5931\u8d25: ' + esc(e.message) + '</div>';
    }
  }
}
var libStatMode = 'all';
function setLibStatMode(m){
  libStatMode = m;
  document.querySelectorAll('#libStatTabs button').forEach(function(b){ b.classList.toggle('active', b.dataset.m === m); });
  if (window.__libStats) renderLibraryStats(window.__libStats);
}
function renderLibraryStats(r){
  window.__libStats = r;
  var m = libStatMode;
  var rows = (r.rows || []).map(function(x){
    return { name: x.name, n: m === 'local' ? x.local : m === 'share' ? x.share : x.total, l: x.local, s: x.share };
  }).filter(function(x){ return x.n > 0; }).sort(function(a, b){ return b.n - a.n; });
  var max = rows.length ? rows[0].n : 1;
  $('libStatsGrid').innerHTML = rows.map(function(row){
    var bar = m === 'all'
      ? '<i class="l" style="width:' + (row.l / max * 100) + '%"></i><i class="s" style="width:' + (row.s / max * 100) + '%"></i>'
      : '<i class="' + (m === 'share' ? 's' : 'l') + '" style="width:' + (row.n / max * 100) + '%"></i>';
    return '<div class="cb" data-name="' + esc(row.name) + '" data-scope="' + m + '" onclick="dashOpenCat(this.dataset.name, this.dataset.scope)"><span class="nm">' + esc(row.name) + '</span><span class="tr">' + bar + '</span><span class="ct">' + row.n.toLocaleString() + '</span><span class="go">›</span></div>';
  }).join('') || '<div class="list-empty">暂无数据</div>';
  var summary = '共 ' + (r.local_total + r.share_total).toLocaleString() + ' 个 STRM · 本地 ' + r.local_total.toLocaleString() + ' / 分享 ' + r.share_total.toLocaleString();
  if (r.local_other + r.share_other > 0) summary += '（未分类 ' + (r.local_other + r.share_other).toLocaleString() + '）';
  $('libStatsSummary').textContent = summary;
}

function dashTotals(d){
  var l = Number(String(d.localCount||0).replace(/[,\s]/g,''))||0, s = Number(String(d.shareCount||0).replace(/[,\s]/g,''))||0, t = l + s;
  $('stat-local').textContent = l.toLocaleString();
  $('stat-share').textContent = s.toLocaleString();
  $('dash-total').textContent = t.toLocaleString();
  $('dash-split-l').style.width = t ? (l / t * 100) + '%' : '0';
  $('dash-split-s').style.width = t ? (s / t * 100) + '%' : '0';
  $('pct-l').textContent = t ? Math.round(l / t * 100) + '%' : '';
  $('pct-s').textContent = t ? Math.round(s / t * 100) + '%' : '';
}
function fmtBig(n){ return n >= 10000 ? (n / 10000).toFixed(2) + '<i class="u">万</i>' : (n || 0).toLocaleString(); }
function fmtUnit(n, u){ return (n || 0).toLocaleString() + '<i class="u">' + u + '</i>'; }
function renderDashEmby(x){
  var note = $('dash-library-facts');
  if (note) {
    var factsTs = x.facts_ts || x.ts || 0;
    var factsTime = factsTs ? new Date(factsTs * 1000).toLocaleString() : '';
    var compareTime = x.ts ? new Date(x.ts * 1000).toLocaleString() : '';
    var stale = x.stale || (x.ts && Date.now() / 1000 - x.ts > 1800);
    note.textContent = factsTime ? '片库缓存 · 集数更新 ' + factsTime
      + (compareTime ? ' · TMDB 对照 ' + compareTime + (stale ? '（已过期）' : '') : '') : '片库缓存 · 更新时间未知';
  }
  $('dash-series').innerHTML = fmtUnit(x.series, '部');
  $('dash-eps').innerHTML = fmtBig(x.eps);
  $('dash-movies').innerHTML = fmtUnit(x.movies, '部');
  var st = x.st || {}, total = x.series || 0;
  var pct = total ? Math.round((st.aligned || 0) / total * 100) : 0;
  $('dash-ring').style.setProperty('--p', pct);
  $('dash-ring-n').textContent = total ? pct + '%' : '—';
  var rows = [['完整', 'ok', st.aligned], ['缺集', 'err', st.missing], ['超集', 'warn', st.extra], ['在更', 'info', st.ongoing], ['未匹配', 'dim', st.unmatched]];
  $('dash-health').innerHTML = rows.map(function(r){
    var n = r[2] || 0;
    return '<div class="hl-row"><b class="dd ' + r[1] + '"></b>' + r[0] + '<span class="cnt">' + n.toLocaleString() + '</span><span class="pc">' + (total ? (n / total * 100).toFixed(1) : '0.0') + '%</span></div>';
  }).join('');
  var top = x.top || [];
  $('dash-miss-note').textContent = st.missing ? ('共 ' + st.missing + ' 部缺集') : '';
  $('dash-missing').innerHTML = top.length ? top.map(function(t){
    var p = t.tot ? Math.min(100, t.have / t.tot * 100) : 0;
    return '<div class="miss" data-name="' + esc(t.name) + '" onclick="dashOpenSeries(this.dataset.name)">'
      + '<div style="flex:1;min-width:0"><div class="nm">' + esc(t.name) + (t.year ? ' (' + t.year + ')' : '') + '</div>'
      + '<div class="bar"><i style="width:' + p + '%"></i></div></div>'
      + '<div class="df">缺 ' + t.diff + ' 集<small>' + t.have + '/' + t.tot + '</small></div></div>';
  }).join('') : '<div class="list-empty">没有缺集的剧集</div>';
}
function dashOpenSeries(name){
  $('embySearch').value = name; embyPage = 1;
  switchTab('mapping');
  if (embyLoaded) renderEmbyList();
}
async function loadDashEmby(){
  var src = null;
  try {
    var r = await api('/api/library/health');
    if (r && r.status === 'success') {
      src = { series: r.series || [], movies: r.movies || [], stats: r.stats || {}, episodes: r.episodes || 0, top_missing: r.top_missing || [],
              ts:r.ts, facts_ts:r.facts_ts, facts_version:r.facts_version, source:r.source, stale:r.stale };
    }
  } catch(e){}
  if (!src && embyLoaded) src = { series: embyData.series, movies: embyData.movies, stats: embyData.stats };
  if (!src) {
    try {
      var r2 = await api('/api/emby/library?force=0&with_tmdb=1&cache_only=1');
      if (r2 && r2.status === 'success') src = { series: r2.series || [], movies: r2.movies || [], stats: md_sync(r2.series || [], r2.stats || {}) };
    } catch(e){}
  }
  if (!src) return;
  var top = src.top_missing || src.series.filter(function(s){ return (s.tmdb_info || {}).match_status === 'missing'; })
    .sort(function(a, b){ return Math.abs((b.tmdb_info || {}).diff || 0) - Math.abs((a.tmdb_info || {}).diff || 0); })
    .slice(0, 6).map(function(s){
      var ti = s.tmdb_info || {};
      return { name: s.name, year: s.year, diff: Math.abs(ti.diff || 0), tot: ti.tmdb_total || 0, have: s.have_eps != null ? s.have_eps : (s.total_episodes || 0) };
    });
  var st = src.stats || {};
  var sum = { series: st.total_series || src.series.length, movies: st.total_movies || src.movies.length, eps: src.episodes != null ? src.episodes : src.series.reduce(function(n,s){ return n + (s.have_eps != null ? s.have_eps : (s.total_episodes || 0)); },0),
              ts:src.ts, facts_ts:src.facts_ts, facts_version:src.facts_version, source:src.source, stale:src.stale,
              st: { aligned: st.aligned || 0, missing: st.missing || 0, extra: st.extra || 0, ongoing: st.ongoing || 0, unmatched: (st.unmatched || 0) + (st.no_tmdb || 0) }, top: top };
  renderDashEmby(sum);
  try { localStorage.setItem('dashEmbyCache', JSON.stringify(sum)); } catch(e){}
}

function renderDashPlanFromDashboard(d){
  try {
    var x = d && d.lastScan;
    if (x) {
      $('dash-loc').textContent = (x.local || 0).toLocaleString();
      $('dash-shr').textContent = (x.share || 0).toLocaleString();
      $('dash-keep').textContent = (x.protected || 0).toLocaleString();
      $('dash-exm').textContent = (x.exempted || 0).toLocaleString();
      $('dash-loc-files').textContent = (x.local_files || 0).toLocaleString() + ' 个 STRM';
      $('dash-shr-files').textContent = (x.share_files || 0).toLocaleString() + ' 个 STRM';
      $('dash-keep-files').textContent = '保护项';
      $('dash-exm-files').textContent = (x.quiet_skipped || 0).toLocaleString() + ' 项静默';
      $('dash-plan-note').textContent = '扫描于 ' + fmtGovAgo(x.age_sec || 0);
      var rc = x.reason_counts || {};
      var labels = {
        share_better:'分享画质更优', local_better:'本地画质更优', share_wins:'分享择优', local_wins:'本地择优',
        special_share_better:'特别篇分享更优', special_local_better:'特别篇本地更优', special_delete_local:'特别篇清理·本地', special_delete_share:'特别篇清理·分享',
        dup_version_local:'本地多版本', dup_version_share:'分享多版本', incomplete_delete_local:'本地残次品', incomplete_delete_share:'分享残次品',
        multi_full_share:'整剧零和·分享替代', multi_protect_partial_share:'多季保护·淘汰分享', multi_protect_partial_local:'多季保护·淘汰本地', decision_keep_local:'保留本地·删分享', decision_keep_share:'保留分享·删本地', whitelist:'白名单豁免', protected:'受保护', exempt:'白名单豁免', unknown:'其他'
      };
      var rows = Object.keys(rc).filter(function(k){ return rc[k] > 0; }).sort(function(a,b){ return rc[b]-rc[a]; }).slice(0,8);
      var totalDecisions = Object.keys(rc).reduce(function(n,k){ return n + (+rc[k] || 0); }, 0);
      $('dash-gov-reason-note').textContent = rows.length ? ('共 ' + totalDecisions + ' 项决策') : '—';
      $('dash-gov-reasons').innerHTML = rows.length ? rows.map(function(k){
        return '<div class="gov-reason"><span>' + esc(labels[k] || k) + '</span><b>' + (+rc[k] || 0).toLocaleString() + '</b></div>';
      }).join('') : '<div class="list-empty">本次扫描没有治理动作</div>';
    } else {
      ['dash-loc','dash-shr','dash-keep','dash-exm'].forEach(function(id){ $(id).textContent = '—'; });
      ['dash-loc-files','dash-shr-files','dash-keep-files','dash-exm-files'].forEach(function(id){ $(id).textContent = '—'; });
      $('dash-plan-note').textContent = '暂无治理扫描';
      $('dash-gov-reasons').innerHTML = '<div class="list-empty">尚无治理扫描</div>';
    }
  } catch(e){}
}
async function loadDashboard(){
  loadStorageStatus();
  loadDashEmby();
  /* 策略快照数据量极小，与 dashboard 并行加载——
     /api/dashboard 要统计全库 STRM（大库较慢），串行会拖得策略快照一直"加载中" */
  var pStrategy = api('/api/strategy').catch(function(e){ console.warn('strategy 失败', e); return null; });
  var pDash = api('/api/dashboard').catch(function(e){ console.warn('dashboard 失败', e); return null; });

  var st = await pStrategy;
  if (st && st.strategy) {
    var s = st.strategy;
    var decisionLabel = { quality_first: '画质优先', keep_local: '保留本地', keep_share: '保留分享', balanced: '严格画质' }[s.decision] || s.decision;
    var spLabel = { keep: '保留', ignore: '忽略', delete: '清理' }[s.special_action] || '保留';
    var mspLabel = { off: '关闭', compare: '开启' }[s.multi_season_protect] || '开启';
    var ex = s.exempt_keywords || [];
    var chips = [['决策', decisionLabel, 'ok'], ['多季保护', mspLabel, 'info'], ['特别篇', spLabel, 'brand'], ['平局', s.tie_keep_local ? '保留本地' : '保留分享', 'dim'], ['剧集达标率', Math.round((Number(s.season_replace_ratio) || 0.9) * 100) + '%', 'dim']];
    $('strategySnapshot').innerHTML = '<div class="chips">' + chips.map(function(c){ return '<span class="sch ' + c[2] + '"><em>' + c[0] + '</em>' + c[1] + '</span>'; }).join('') + '</div>'
      + (ex.length ? '<div class="sch-ex">白名单：' + ex.map(function(k){ return esc(k); }).join('、') + '</div>' : '');
    $('dash-exempt').textContent = ex.length + ' 条';
  }

  var d = await pDash;
  if (d) {
    renderDashboard(d);
    renderDashPlanFromDashboard(d);
    saveDashboardCache(d);
  }
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
  try { var r = await api('/api/storage/status'); if (r.status === 'success') renderStorageStatus(r.storage); } catch(e){}
}
function renderDashboard(d){
    renderSidebarServices(d.services);
    if (d.storage) renderStorageStatus(d.storage);
    $('stat-local').textContent = d.localCount || '-';
    $('stat-share').textContent = d.shareCount || '-';
    dashTotals(d);
    /* 侧边栏版本号跟随后端（之前硬编码 V1.1 不同步） */
    if (d.version) {
      var sv = $('sideVersion'); if (sv) sv.textContent = 'V' + d.version;
      var hv = $('headerVersion'); if (hv) hv.textContent = 'v' + d.version;
    }
    var embyOk = d.services && d.services.emby && d.services.emby.ok;
    $('svc-emby').innerHTML = embyOk ? statusOk('在线') : statusNo('离线');
    $('svc-emby-host').textContent = (d.services && d.services.emby && d.services.emby.host) || '—';
    /* 缓存 Emby 主机地址，供 showEmbyDetailById 里的"在 Emby 中打开"链接使用 */
    if (d.services && d.services.emby && d.services.emby.host) {
      window.__embyHost = d.services.emby.host;
    }
    var tmdbOk = d.services && d.services.tmdb && d.services.tmdb.ok;
    $('svc-tmdb').innerHTML = tmdbOk ? statusOk('已配置') : statusNo('未配置');
    $('svc-tmdb-sub').textContent = tmdbOk ? '影视探索可用' : '去「规则设置」配置';

    if (d.subscriptions) {
      var ss = d.subscriptions;
      $('stat-sub').textContent = (ss.enabled || 0) + ' / ' + (ss.total || 0) + ' 部';
      $('stat-sub-sub').textContent = ({running:'检查中',error:'检查失败',partial:'部分失败',disabled:'已停用',success:'最近检查正常'})[(ss.check || {}).status] || ((ss.enabled || 0) > 0 ? '等待检查' : '点击查看');
    }
    if (d.morningReport) {
      var mr = d.morningReport;
      $('stat-morning').textContent = mr.enabled ? mr.time : '未开启';
      $('stat-morning-sub').textContent = mr.last_date ? ('上次 ' + mr.last_date) : ('预扫 ' + (mr.prescan_min || 5) + ' 分钟');
    }
    if (d.ingest) {
      var ing = d.ingest;
      $('stat-ingest-mov').textContent = (ing.movies || 0) + ' / ' + (ing.series || 0) + ' 部';
      var age = ing.cache_age_sec;
      if (age == null) $('stat-ingest-age').textContent = '无缓存';
      else if (age < 60) $('stat-ingest-age').textContent = age + ' 秒前';
      else if (age < 3600) $('stat-ingest-age').textContent = Math.floor(age/60) + ' 分钟前';
      else $('stat-ingest-age').textContent = Math.floor(age/3600) + ' 小时前';
    }
}


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
