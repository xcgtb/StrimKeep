/* ═══════════ 片库映射 ═══════════ */
var embyData = { series: [], movies: [], stats: {} };
var embyFilterState = { type: 'series', filter: 'all', scope: 'all', cat: '' };
function catText(x){
  var skip = { name:1, title:1, overview:1, desc:1, tmdb_info:1, id:1 }, out = [];
  for (var k in x) {
    if (skip[k]) continue;
    var v = x[k];
    if (typeof v === 'string') out.push(v);
    else if (Array.isArray(v)) v.forEach(function(i){ if (typeof i === 'string') out.push(i); });
  }
  return out.join('|');
}
function catClean(n){ return String(n || '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g, ''); }
function catSeg(x, c){ return String(x.path || '').split(/[\\/]+/).some(function(g){ return catClean(g) === c; }); }
/* 与后端 _recompute_all_stats 的 CATS 同一套规则：路径逐级目录名 包含 关键词 即归入该分类（先命中的目录段为准） */
var CAT_KW = {
  '儿童节目': ['儿童'], '演唱会': ['演唱会'], '综艺剧': ['综艺'], '动漫剧': ['动漫'], '动画电影': ['动画电影'], '纪录片': ['纪录'],
  '国产剧': ['国产剧集', '国产剧'], '欧美剧': ['欧美剧集', '欧美剧', '其他剧集'], '日韩剧': ['日韩剧集', '日韩剧'],
  '华语电影': ['国产电影', '华语电影'], '外语电影': ['欧美电影', '日韩电影', '外语电影', '其他电影']
};
function catOf(x){
  if (x._cat !== undefined) return x._cat;
  var segs = String(x.path || '').split(/[\\/]+/), names = Object.keys(CAT_KW), r = '';
  for (var i = 0; i < segs.length && !r; i++) {
    for (var j = 0; j < names.length; j++) {
      if (CAT_KW[names[j]].some(function(k){ return segs[i].indexOf(k) >= 0; })) { r = names[j]; break; }
    }
  }
  x._cat = r; return r;
}
function catHit(x, c){
  if (CAT_KW[c]) return catOf(x) === c;
  return embyFilterState.catMode === 'sub' ? catText(x).indexOf(c) >= 0 : catSeg(x, c);
}
function catApply(arr){
  var c = embyFilterState.cat; if (!c) return arr;
  return arr.filter(function(x){ return catHit(x, c); });
}
/* ── 手动完结：TMDB 季数/集数不准时，人工标记某部剧「已完结」，不再计入缺集 / 在更 ── */
function mdGet(){ return window.__mdSet || {}; }
async function mdLoad(){
  try {
    var r = await api('/api/manual_done');
    window.__mdSet = (r && r.items) || {};
    /* 迁移：早期版本把标记存在浏览器里，首次同步到服务器 */
    var old = {}; try { old = JSON.parse(localStorage.getItem('manualDone') || '{}'); } catch(e){}
    var ids = Object.keys(old).filter(function(k){ return !window.__mdSet[k]; });
    for (var i = 0; i < ids.length; i++) {
      var q = await api('/api/manual_done', { method: 'POST', body: JSON.stringify({ id: ids[i], name: (old[ids[i]] || {}).n || '', done: true }) });
      if (q && q.items) window.__mdSet = q.items;
    }
    if (Object.keys(old).length) { try { localStorage.removeItem('manualDone'); } catch(e){} }
  } catch(e){ window.__mdSet = window.__mdSet || {}; }
}
function md_sync(series, base){
  var set = mdGet(), st = {}, k;
  for (k in (base || {})) st[k] = base[k];
  series.forEach(function(x){
    var ti = x.tmdb_info; if (!ti) return;
    if (set[x.id]) {
      if (!x._mdOrig && (ti.match_status === 'missing' || ti.match_status === 'ongoing')) x._mdOrig = { st: ti.match_status, diff: ti.diff };
      if (x._mdOrig) {
        st[x._mdOrig.st] = Math.max(0, (st[x._mdOrig.st] || 0) - 1);
        st.aligned = (st.aligned || 0) + 1;
        ti.match_status = 'aligned'; ti.diff = 0; x._md = true;
      }
    } else if (x._mdOrig) {
      ti.match_status = x._mdOrig.st; ti.diff = x._mdOrig.diff; delete x._mdOrig; x._md = false;
    }
  });
  return st;
}
async function toggleManualDone(id){
  var x = (window.__embySeriesMap || {})[id]; if (!x) return;
  var on = !mdGet()[id];
  try {
    var r = await api('/api/manual_done', { method: 'POST', body: JSON.stringify({ id: id, name: x.name || '', done: on }) });
    if (r.status !== 'success') throw new Error(r.message || '保存失败');
    window.__mdSet = r.items || {};
  } catch(e){ toast('保存失败：' + e.message, 'error', 3200); return; }
  embyData.stats = md_sync(embyData.series, embyData.statsBase || embyData.stats);
  updateEmbyChips();
  embyPage = 1; renderEmbyList();
  showEmbyDetailById(id);
  toast(on ? '已标记完结，不再计入缺集' : '已取消完结，恢复 TMDB 对照结果', 'success', 2600);
  if (typeof loadDashEmby === 'function') loadDashEmby();
}
function clearEmbyCat(){ embyFilterState.cat = ''; embyPage = 1; renderEmbyList(); }
function syncCatChip(){
  var c = embyFilterState.cat, box = $('embyCatChip');
  if (!box) return;
  box.hidden = !c; $('embyCatName').textContent = c ? '分类：' + c : '';
}
function dashOpenCat(name, scope){
  name = catClean(name);
  embyFilterState.cat = name; embyFilterState.filter = 'all'; embyFilterState.scope = (scope === 'local' || scope === 'share') ? scope : 'all';
  embyFilterState.catMode = 'seg';
  var hasS = embyData.series.some(function(x){ return catHit(x, name); });
  var hasM = embyData.movies.some(function(x){ return catHit(x, name); });
  if (!hasS && !hasM && (embyData.series.length || embyData.movies.length)) {
    embyFilterState.catMode = 'sub';
    hasS = embyData.series.some(function(x){ return catText(x).indexOf(name) >= 0; });
    hasM = embyData.movies.some(function(x){ return catText(x).indexOf(name) >= 0; });
  }
  embyFilterState.type = (!hasS && hasM) ? 'movies' : 'series';
  $('embySearch').value = ''; embyPage = 1;
  switchTab('mapping');
  setEmbyType(embyFilterState.type);
  document.querySelectorAll('#embyFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === 'all'); });
  document.querySelectorAll('#embyScopeFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.s === embyFilterState.scope); });
}

function setEmbyScope(sc){
  embyPage = 1;
  embyFilterState.scope = sc;
  document.querySelectorAll('#embyScopeFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.s === sc); });
  renderEmbyList();
}
var embyPage = 1;
var EMBY_PAGE = 40;
var embyView = { arr: [], fn: null };
var embyShown = 0;
var mappingPrefetch = null;
var embyLoaded = false;
var embyLibraryLoading = false, embyLibraryGeneration = 0;
function ensureEmbyLoaded(){ if (!embyLoaded && !embyLibraryLoading) loadEmbyLibrary(false, true, true); }
var tmdbProgressTimer = null;
var tmdbProgressGeneration = 0;

async function pollTmdbProgress(){
  var generation = tmdbProgressGeneration;
  var watching = !!tmdbProgressTimer;
  try {
    var r = await api('/api/tmdb/progress');
    if (generation !== tmdbProgressGeneration) return;
    var p = r.progress || {};
    if (p.running) {
      document.getElementById('tmdbProgressWrap').classList.remove('hidden');
      document.getElementById('tmdbProgressFill').style.width = p.percent + '%';
      var stage = p.stage || '对照中...';
      if (p.total > 0) stage += ' (' + p.done + '/' + p.total + ')';
      document.getElementById('tmdbProgressStage').textContent = stage;
      var etaText = '';
      if (p.elapsed_sec) {
        etaText = '已用 ' + p.elapsed_sec + 's';
        if (p.eta_sec != null && p.eta_sec > 0) etaText += ' · 预计还需 ' + p.eta_sec + 's';
      }
      document.getElementById('tmdbProgressEta').textContent = etaText;
    } else {
      document.getElementById('tmdbProgressWrap').classList.add('hidden');
      if (tmdbProgressTimer) { clearInterval(tmdbProgressTimer); tmdbProgressTimer = null; }
      if (p.finished_at && !p.error) loadEmbyLibrary(false, true);
      else if (p.error && watching) toast('TMDB 对照失败: ' + p.error, 'error', 5000);
    }
  } catch(e) {}
}

function startTmdbProgressWatch(){
  if (tmdbProgressTimer) return;
  tmdbProgressTimer = setInterval(pollTmdbProgress, 2000);
  pollTmdbProgress();
}

async function loadEmbyLibrary(force, withTmdb, cacheOnly){
  var generation = ++embyLibraryGeneration;
  embyLibraryLoading = true;
  // 数据准备好再替换；重新对照及请求失败时保留已有海报墙。
  if (force && withTmdb) {
    // 本次启动前停止旧轮询；晚到的旧进度不能中断新任务或弹出旧错误。
    ++tmdbProgressGeneration;
    if (tmdbProgressTimer) { clearInterval(tmdbProgressTimer); tmdbProgressTimer = null; }
    try {
      var r0 = await api('/api/emby/library?force=1&with_tmdb=1');
      if (generation !== embyLibraryGeneration) return;
      if (r0 && (r0.status === 'started' || r0.status === 'running')) {
        if (!embyLoaded) $('embyList').innerHTML = '<div class="list-empty">TMDB 对照进行中，请稍候...</div>';
        embyLibraryLoading = false;
        startTmdbProgressWatch();
        return;
      }
      throw new Error((r0 && r0.message) || '启动失败');
    } catch(e) {
      if (generation !== embyLibraryGeneration) return;
      embyLibraryLoading = false;
      toast('TMDB 对照启动失败: ' + e.message, 'error', 5000);
      return;
    }
  }
  var msg = cacheOnly ? '正在读取片库缓存...' : (withTmdb ? '正在拉取 Emby + TMDB（首次可能较慢）...' : '正在拉取 Emby 库...');
  if (!embyLoaded) $('embyList').innerHTML = '<div class="list-empty">' + msg + '</div>';
  try {
    var url = '/api/emby/library?force=' + (force ? 1 : 0) + '&with_tmdb=' + (withTmdb ? 1 : 0) + (cacheOnly ? '&cache_only=1' : '');
    var r = await api(url);
    if (generation !== embyLibraryGeneration) return;
    // 没有对照缓存（首次使用 / 缓存太旧）：退回快速模式，只拉 Emby 库，不触发 TMDB 对照
    if (cacheOnly && r.status === 'nocache') return loadEmbyLibrary(false, false);
    if (r.status !== 'success') throw new Error(r.message || '失败');
    if (cacheOnly && r.ts) toast('已载入片库缓存（' + fmtAgo(r.ts) + '对照），需要最新数据请点「对照 TMDB」', 'success', 3200);
    await mdLoad();
    if (generation !== embyLibraryGeneration) return;
    embyPage = 1;
    embyShown = 0;
    mappingPrefetch = null;
    embyData.series = r.series || [];
    embyData.movies = r.movies || [];
    embyData.statsBase = r.stats || {};
    embyData.stats = md_sync(embyData.series, embyData.statsBase);
    /* 缓存 Emby 主机地址（后端在返回里带了 emby_host），供"在 Emby 中打开"链接使用 */
    if (r.emby_host) window.__embyHost = r.emby_host;
    window.__embySeriesMap = {};
    embyData.series.forEach(function(x){ if (x.id) window.__embySeriesMap[x.id] = x; });
    window.__embyMovieMap = {};
    embyData.movies.forEach(function(x){ if (x.id) window.__embyMovieMap[x.id] = x; });
    embyLoaded = true;
    var st = embyData.stats;
    $('embyStats').style.display = '';
    $('embyStats2').style.display = '';
    $('embyTitle1').style.display = '';
    $('embyTitle2').style.display = '';
    $('emby-title-count').textContent = st.total_series || 0;
    $('emby-title-movies').textContent = st.total_movies || 0;
    $('emby-total').textContent      = st.total_series || 0;
    $('emby-aligned').textContent    = st.aligned || 0;
    $('emby-missing').textContent    = st.missing || 0;
    $('emby-extra').textContent      = st.extra || 0;
    $('emby-ongoing').textContent    = st.ongoing || 0;
    $('emby-unmatched').textContent  = (st.unmatched || 0) + (st.no_tmdb || 0);
    $('emby-movies').textContent     = st.total_movies || 0;
    $('emby-tmdb-errors').textContent = r.tmdb_errors || 0;
    updateEmbyChips();
    renderEmbyList();
  } catch(e){
    if (generation !== embyLibraryGeneration) return;
    if (embyLoaded) toast('片库更新失败，保留当前列表：' + e.message, 'error', 4000);
    else $('embyList').innerHTML = '<div class="list-empty">' + esc(e.message) + '</div>';
  } finally {if (generation === embyLibraryGeneration) embyLibraryLoading = false;}
}
var embyMoreLoading = false;
var embyMoreFailed = false;
function loadMoreEmby(){ return embyMore(); }
async function embyMore(){
  if (embyMoreLoading || !embyView.fn || embyShown >= embyView.arr.length) return;
  var view = embyView, from = embyShown;
  embyMoreLoading = true;
  embyMoreFailed = false;
  updateEmbyLoadMore(view.arr.length);
  try {
    // 给按钮一次绘制机会；图片请求不阻塞分页按钮或改变已有卡片。
    await new Promise(function(resolve){requestAnimationFrame(function(){setTimeout(resolve,0);});});
    if (view !== embyView || from !== embyShown) return;
    var nextShown = Math.min(view.arr.length, posterNextCount(from, $('embyList')));
    var html = view.arr.slice(from, nextShown).map(view.fn).join('');
    var list = $('embyList');
    list.insertAdjacentHTML('beforeend', html);
    embyPage += 1;
    embyShown = nextShown;
    hydratePosters(list);
  } catch(e) {
    embyMoreFailed = true;
    toast('加载失败，可重试：' + e.message, 'error', 4000);
  } finally {
    embyMoreLoading = false;
    updateEmbyLoadMore(embyView.arr.length);
    schedulePosterAutoLoad();
  }
}
function updateEmbyLoadMore(total){
  var wrap = $('embyLoadMoreWrap'), button = $('embyMoreBtn');
  if (!wrap) return;
  var shown = Math.min(total, embyShown);
  wrap.style.display = total ? '' : 'none';
  $('embyInfText').textContent = '已显示 ' + shown + ' / ' + total + ' 部 · 按整行追加'
    + (shown >= total ? ' · 全部加载完毕' : '');
  if (button) {
    button.hidden = shown >= total;
    button.disabled = embyMoreLoading;
    button.textContent = embyMoreLoading ? '加载中...' : (embyMoreFailed ? '重试加载下一批' : '加载更多');
  }
  prefetchMappingPosters();
}
function updateEmbyChips(){
  var st = embyData.stats || {};
  var m = { 'mc-series': st.total_series, 'mc-all': st.total_series, 'mc-movies': st.total_movies, 'mc-aligned': st.aligned,
            'mc-missing': st.missing, 'mc-extra': st.extra, 'mc-ongoing': st.ongoing, 'mc-unmatched': (st.unmatched || 0) + (st.no_tmdb || 0) };
  for (var k in m) { var e = $(k); if (e) e.textContent = (m[k] || 0).toLocaleString(); }
}

function setEmbyType(t){
  embyPage = 1;
  embyFilterState.type = t;
  document.querySelectorAll('#embyTypeFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.t === t); });
  var f = $('embyFilter');
  if (t === 'movies') {
    for (var i = 1; i <= 4; i++) f.children[i].style.display = 'none';
    if (embyFilterState.filter !== 'all') {
      embyFilterState.filter = 'all';
      document.querySelectorAll('#embyFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === 'all'); });
    }
  } else {
    for (var i = 1; i <= 4; i++) f.children[i].style.display = '';
  }
  renderEmbyList();
}
function setEmbyFilter(f){
  embyPage = 1;
  embyFilterState.filter = f;
  document.querySelectorAll('#embyFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === f); });
  renderEmbyList();
}
function renderEmbyList(){
  var list = $('embyList');
  syncCatChip();
  embyMoreFailed = false;
  var keyword = ($('embySearch').value || '').trim().toLowerCase();
  var arr;
  if (embyFilterState.type === 'movies') {
    arr = embyData.movies.slice();
    if (embyFilterState.scope === 'local') arr = arr.filter(function(x){ return x.in_local; });
    else if (embyFilterState.scope === 'share') arr = arr.filter(function(x){ return x.in_share; });
    arr = catApply(arr);
    if (keyword) arr = arr.filter(function(x){ return (x.name||'').toLowerCase().indexOf(keyword) >= 0; });
    if (!arr.length) { embyView = { arr: [], fn: null }; updateEmbyLoadMore(0); list.innerHTML = '<div class="list-empty">' + (embyFilterState.cat ? '没有匹配「' + esc(embyFilterState.cat) + '」的条目（可先点一次「重新对照」刷新片库数据）' : '无匹配项') + '</div>'; return; }
    embyView = { arr: arr, fn: renderMovieCard };
    embyShown = embyPage === 1 ? Math.min(arr.length, posterNextCount(0, list)) : Math.min(arr.length, embyShown);
    list.innerHTML = arr.slice(0, embyShown).map(renderMovieCard).join('');
    hydratePosters(list);
    updateEmbyLoadMore(arr.length);
    schedulePosterAutoLoad();
    return;
  }
  arr = embyData.series.slice();
  if (embyFilterState.filter === 'unmatched') {
    arr = arr.filter(function(x){
      var st = (x.tmdb_info || {}).match_status;
      return st === 'unmatched' || st === 'no_tmdb';
    });
  } else if (embyFilterState.filter !== 'all') {
    arr = arr.filter(function(x){ return (x.tmdb_info || {}).match_status === embyFilterState.filter; });
  }
  if (embyFilterState.scope === 'local') arr = arr.filter(function(x){ return x.in_local; });
  else if (embyFilterState.scope === 'share') arr = arr.filter(function(x){ return x.in_share; });
  arr = catApply(arr);
    if (keyword) arr = arr.filter(function(x){ return (x.name||'').toLowerCase().indexOf(keyword) >= 0; });
  if (!arr.length) { embyView = { arr: [], fn: null }; updateEmbyLoadMore(0); list.innerHTML = '<div class="list-empty">' + (embyFilterState.cat ? '没有匹配「' + esc(embyFilterState.cat) + '」的条目（若一直为空，说明 Emby 数据里没有分类/路径字段）' : '无匹配项') + '</div>'; return; }
  var prio = { missing: 0, extra: 1, unmatched: 2, no_tmdb: 2, ongoing: 3, aligned: 4 };
  arr.sort(function(a, b){
    var sa = (a.tmdb_info || {}).match_status || 'unmatched';
    var sb = (b.tmdb_info || {}).match_status || 'unmatched';
    var pa = prio[sa] != null ? prio[sa] : 9;
    var pb = prio[sb] != null ? prio[sb] : 9;
    if (pa !== pb) return pa - pb;
    var da = Math.abs((a.tmdb_info || {}).diff || 0);
    var db = Math.abs((b.tmdb_info || {}).diff || 0);
    if (da !== db) return db - da;
    return (a.name || '').localeCompare(b.name || '');
  });
  embyView = { arr: arr, fn: renderSeriesCard };
  embyShown = embyPage === 1 ? Math.min(arr.length, posterNextCount(0, list)) : Math.min(arr.length, embyShown);
  list.innerHTML = arr.slice(0, embyShown).map(renderSeriesCard).join('');
  hydratePosters(list);
  updateEmbyLoadMore(arr.length);
  schedulePosterAutoLoad();
}
function toggleSeasonList(id){
  var el = document.getElementById(id);
  if (!el) return;
  var full = el.querySelector('.season-list-full');
  var otherRows = Array.prototype.slice.call(el.children).filter(function(c){ return c !== full; });
  var collapsed = el.getAttribute('data-collapsed') !== '0';
  otherRows.forEach(function(r){ r.classList.toggle('hidden', collapsed); });
  if (full) full.classList.toggle('hidden', !collapsed);
  el.setAttribute('data-collapsed', collapsed ? '0' : '1');
}
function renderSeriesCard(s){
  var ti = s.tmdb_info || {};
  var mst = ti.match_status || 'unmatched';

  var statusText = '', statusCls = 'no';
  if (mst === 'aligned')        { statusText = '✓ 完整'; statusCls = 'ok'; }
  else if (mst === 'missing')   { statusText = '缺 ' + Math.abs(ti.diff || 0) + ' 集'; statusCls = 'local'; }
  else if (mst === 'extra')     { statusText = '超 ' + (ti.diff || 0) + ' 集'; statusCls = 'share'; }
  else if (mst === 'ongoing')   { statusText = '⏳ 在更'; statusCls = 'ok'; }
  else if (mst === 'unmatched') { statusText = '未匹配'; statusCls = 'no'; }
  else if (mst === 'no_tmdb')   { statusText = '无 TMDB'; statusCls = 'no'; }
  else if (mst === 'pending')   { statusText = '待对照'; statusCls = 'no'; }

  if (s._md) { statusText = '✓ 完整'; statusCls = 'ok'; }

  var libTag = '';
  if (s.in_local && s.in_share) libTag = '双库';
  else if (s.in_local) libTag = '本地';
  else if (s.in_share) libTag = '分享';

  /* 照搬上游 TgtoDrive 海报对照逻辑：TMDB poster_path 优先（官方最新图），Emby 缓存图兜底 */
  var tposter = ti && ti.poster ? '/api/tmdb/poster' + ti.poster : '';
  var poster = tposter
    ? '<div class="poster-loading">海报加载中</div><img data-emby-src="' + esc(tposter) + '" loading="lazy" decoding="async" onload="clearPosterLoading(this)" onerror="clearPosterLoading(this);this.onerror=null;this.style.display=\'none\';var n=this.nextElementSibling;if(n)n.style.display=\'flex\';"><div class="no-img" style="display:none">暂无海报</div>'
    : (s.has_image
      ? '<div class="poster-loading">海报加载中</div><img data-emby-src="/api/emby/poster/' + esc(s.id) + '" loading="lazy" decoding="async" onload="clearPosterLoading(this)">'
      : '<div class="no-img">暂无海报</div>');

  var epInfo = (s.total_seasons || 0) + ' 季 · ' + libraryEpisodeCount(s) + ' 集';
  // 入库进度：已入库 have/total（TMDB 总数），分库时显示 本地/分享
  var tmdbTotal = libraryEpisodeTotal(ti);
  var have = libraryEpisodeCount(s);
  if (tmdbTotal > 0) {
    epInfo = '已入库 ' + have + '/' + tmdbTotal + ' 集';
  } else if (s.in_local && s.in_share) {
    epInfo = '本地 ' + (s.local_eps || 0) + ' / 分享 ' + (s.share_eps || 0) + ' 集';
  } else {
    epInfo = (s.total_seasons || 0) + ' 季 · ' + libraryEpisodeCount(s) + ' 集';
  }

  return '<div class="poster-card emby-poster-card st-' + mst + '" onclick="showEmbyDetailById(' + jsarg(s.id) + ')">'
    + '<div class="poster-wrap">' + poster
    + '<span class="badge-type">剧集</span>'
    + '<span class="badge-status ' + statusCls + '">' + statusText + '</span>'
    + '</div>'
    + '<div class="info">'
    + '<div class="t" title="' + esc(s.name || '?') + '">' + esc(s.name || '?') + '</div>'
    + '<div class="meta"><span>' + (s.year || '—') + ' · ' + libTag + '</span></div>'
    + '<div class="meta"><span style="color:var(--text-dim);font-size:10px">' + epInfo + '</span></div>'
    + '</div>'
    + '</div>';
}

function showMovieDetailById(id){
  var m = (window.__embyMovieMap || {})[id];
  if (!m) return;
  var html = '';
  html += '<div style="display:flex;gap:16px;margin-bottom:16px">';
  html += '<div style="width:110px;height:165px;border-radius:8px;background:#f3f4f6;overflow:hidden;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:11px">';
  /* TMDB poster_path 优先（上游海报对照逻辑），Emby 缓存图兜底 */
  var mtp = m.poster_tmdb ? '/api/tmdb/poster' + m.poster_tmdb : '';
  html += mtp
    ? '<img data-emby-src="' + esc(mtp) + '" style="width:100%;height:100%;object-fit:cover" onerror="this.onerror=null;this.style.display=\'none\';">'
    : (m.has_image ? '<img data-emby-src="/api/emby/poster/' + esc(m.id) + '" style="width:100%;height:100%;object-fit:cover">' : '\u65e0\u56fe');
  html += '</div>';
  html += '<div style="flex:1;min-width:0">';
  html += '<h3 style="margin:0 0 8px;font-size:16px;line-height:1.3;word-break:break-word">' + esc(m.name || '?') + '</h3>';
  html += '<div class="emby-badges">';
  if (m.in_local && m.in_share) html += '<span class="emby-badge loc">\u672c\u5730+\u5206\u4eab</span>';
  else if (m.in_local) html += '<span class="emby-badge loc">\u672c\u5730</span>';
  else if (m.in_share) html += '<span class="emby-badge shr">\u5206\u4eab</span>';
  if (m.year) html += '<span class="emby-badge gen">' + m.year + '</span>';
  html += '</div>';
  html += '<div style="margin-top:8px;font-size:12px;color:#6b7280">\u7535\u5f71</div>';
  html += '</div></div>';

  var embyHost = (window.__embyHost || '').replace(/\/+$/, '');
  html += '<div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">';
  if (m.tmdb_id) {
    html += '<button class="btn red" onclick="deleteMovieByTmdb(' + jsarg(m.tmdb_id) + ',\'local\',' + jsarg(m.name || '') + ')">\u5220\u9664\u672c\u5730\u5e93</button>';
    html += '<button class="btn red" onclick="deleteMovieByTmdb(' + jsarg(m.tmdb_id) + ',\'share\',' + jsarg(m.name || '') + ')">\u5220\u9664\u5206\u4eab\u5e93</button>';
  }
  if (m.id && embyHost) {
    html += '<a href="' + esc(embyHost) + '/web/index.html#!/item?id=' + esc(m.id) + '" target="_blank" rel="noopener" style="text-decoration:none" class="btn gray">\u5728 Emby \u4e2d\u6253\u5f00</a>';
  }
  html += '<button class="btn gray" onclick="closeModal()">\u5173\u95ed</button>';
  html += '</div>';

  openModal(html);
  hydratePosters($('modalBody'));
}

async function deleteMovieByTmdb(tmdbId, target, movieName){
  var targetName = target === 'local' ? '\u672c\u5730\u5e93\uff08\u542b 115 \u6e90\u6587\u4ef6\uff09' : '\u5206\u4eab\u5e93';
  try {
    var r1 = await api('/api/emby/movie/delete_by_tmdb', {
      method: 'POST',
      body: JSON.stringify({ tmdb_id: tmdbId, target: target, dry_run: true, name: movieName })
    });
    if (r1.status !== 'success') throw new Error(r1.message || '\u5931\u8d25');
    var count = r1.count || 0;
    if (count === 0) {
      alert('\u8be5\u5e93\u65e0\u6b64\u7535\u5f71\u6587\u4ef6');
      return;
    }
    var msg = '\u26a0\ufe0f \u786e\u8ba4\u5220\u9664\u3010' + targetName + '\u3011\uff1f\n\n' +
              '\u7535\u5f71\uff1a' + (movieName || '') + '\n' +
              '\u5c06\u5220\u9664 ' + count + ' \u4e2a\u6587\u4ef6' +
              (target === 'local' ? '\uff0c\u5e76\u8054\u52a8\u5220\u9664 115 \u7f51\u76d8\u4e0a\u7684\u6e90\u6587\u4ef6' : '') +
              '\n\n\u6b64\u64cd\u4f5c\u4e0d\u53ef\u6062\u590d\uff01';
    if (!confirm(msg)) return;

    var r2 = await api('/api/emby/movie/delete_by_tmdb', {
      method: 'POST',
      body: JSON.stringify({ tmdb_id: tmdbId, target: target, dry_run: false, name: movieName })
    });
    if (r2.status !== 'success') throw new Error(r2.message || '\u5931\u8d25');

    var flag = target === 'local' ? 'in_local' : 'in_share';
    embyData.movies = embyData.movies.filter(function(x){
      return !(String(x.tmdb_id || '') === String(tmdbId) && x[flag]);
    });
    if (embyData.stats) embyData.stats.total_movies = embyData.movies.length;
    renderEmbyList();
    closeModal();
    toast('\u2705 \u5df2\u5220\u9664 strm ' + (r2.strm_removed || 0) + ' \u4e2a' +
          (target === 'local' ? '\uff0c\u4e91\u7aef ' + (r2.cloud_removed || 0) + ' \u4e2a' : ''), 'success', 3500);
    if (r2.errors && r2.errors.length) {
      setTimeout(function(){ alert('\u26a0\ufe0f \u90e8\u5206\u6587\u4ef6\u672a\u5220\u9664\uff1a\n' + r2.errors.slice(0, 3).join('\n')); }, 50);
    }
  } catch(e){
    alert('\u5220\u9664\u5931\u8d25\uff1a' + e.message);
  }
}

function showEmbyDetailById(id){
  var s = (window.__embySeriesMap || {})[id];
  if (!s) return;
  var ti = s.tmdb_info || {};
  var mst = ti.match_status || 'unmatched';
  var html = '<div class="mapping-detail">';

  html += '<div class="mapping-detail-header">';
  html += '<div class="mapping-detail-poster">';
  /* TMDB poster_path 优先（上游海报对照逻辑），Emby 缓存图兜底 */
  html += ti.poster
    ? '<img data-emby-src="/api/tmdb/poster' + esc(ti.poster) + '" style="width:100%;height:100%;object-fit:cover" onerror="this.onerror=null;this.style.display=\'none\';">'
    : (s.has_image
      ? '<img data-emby-src="/api/emby/poster/' + esc(s.id) + '" style="width:100%;height:100%;object-fit:cover">'
      : '无图');
  html += '</div>';
  html += '<div class="mapping-detail-info">';
  html += '<h3>' + esc(s.name || '?') + '</h3>';
  html += '<div class="emby-badges">';
  if (s.in_local && s.in_share) html += '<span class="emby-badge loc">本地+分享</span>';
  else if (s.in_local) html += '<span class="emby-badge loc">Emby</span>';
  else if (s.in_share) html += '<span class="emby-badge shr">分享</span>';
  if (s._md) html += '<span class="emby-badge ok" title="已手动标记完结">✓ 完整</span>';
  else if (mst === 'aligned') html += '<span class="emby-badge ok">✓ 完整</span>';
  else if (mst === 'missing') html += '<span class="emby-badge miss">缺 ' + Math.abs(ti.diff || 0) + ' 集</span>';
  else if (mst === 'extra') html += '<span class="emby-badge extra">超 ' + (ti.diff || 0) + ' 集</span>';
  else if (mst === 'ongoing') html += '<span class="emby-badge ongoing">在更</span>';
  else if (mst === 'unmatched') html += '<span class="emby-badge dim">未匹配</span>';
  else if (mst === 'no_tmdb') html += '<span class="emby-badge dim">无 TMDB</span>';
  else if (mst === 'pending') html += '<span class="emby-badge gen">待对照</span>';
  if (s.year) html += '<span class="emby-badge gen">' + s.year + '</span>';
  html += '</div>';
  html += '<div class="mapping-detail-facts">';
  if (libraryEpisodeTotal(ti) > 0) {
    var have = libraryEpisodeCount(s);
    html += '已入库 ' + have + '/' + libraryEpisodeTotal(ti) + ' 集';
    html += ' <span>（片库缓存）</span>';
  } else if (s.in_local && s.in_share) {
    html += '本地 ' + (s.local_eps || 0) + ' / 分享 ' + (s.share_eps || 0) + ' 集';
  } else {
    html += 'Emby ' + (s.total_seasons || 0) + ' 季 · ' + libraryEpisodeCount(s) + ' 集';
  }
  html += '<div style="margin-top:6px;font-size:12px;color:var(--text-mid)">' + esc(libraryFactsLabel(s)) + '</div>';
  html += '</div>';
  html += '</div></div>';

  if (s._md) html += '<div style="margin-top:12px;font-size:12px;color:var(--text-dim)">已手动标记为完结：不再计入缺集 / 在更，下方两库分集明细仍按 TMDB 原始数据显示。标记保存在服务器，所有设备通用。</div>';
  html += '<div id="libBreakdown" class="lib-breakdown">正在读取两库分集...</div>';

  /* "在 Emby 中打开"链接改为读配置，不再硬编码 IP */
  var embyHost = (window.__embyHost || '').replace(/\/+$/, '');
  html += '<div class="mapping-detail-actions">';
  if (s._md) html += '<button class="btn gray" onclick="toggleManualDone(' + jsarg(s.id) + ')">取消完结</button>';
  else if (mst === 'missing' || mst === 'ongoing') html += '<button class="btn" onclick="toggleManualDone(' + jsarg(s.id) + ')">手动完结</button>';
  html += '<button class="btn mapping-delete" onclick="deleteSeriesFromLib(' + jsarg(s.id) + ',\'local\',' + jsarg(s.name || '') + ')">删除本地库</button>';
  html += '<button class="btn mapping-delete" onclick="deleteSeriesFromLib(' + jsarg(s.id) + ',\'share\',' + jsarg(s.name || '') + ')">删除分享库</button>';
  if (s.id && embyHost) {
    html += '<a href="' + esc(embyHost) + '/web/index.html#!/item?id=' + esc(s.id) + '" target="_blank" rel="noopener" style="text-decoration:none" class="btn gray">在 Emby 中打开</a>';
  }
  html += '<button class="btn gray" onclick="closeModal()">关闭</button>';
  html += '</div></div>';

  openModal(html);
  hydratePosters($('modalBody'));
  loadLibBreakdown(s, ti);
}

async function deleteSeriesFromLib(seriesId, target, seriesName){
  var targetName = target === 'local' ? '\u672c\u5730\u5e93\uff08\u542b 115 \u6e90\u6587\u4ef6\uff09' : '\u5206\u4eab\u5e93';
  try {
    // 1. dry_run 先拿数量
    var r1 = await api('/api/emby/series/' + seriesId + '/delete', {
      method: 'POST',
      body: JSON.stringify({ target: target, dry_run: true, series_name: seriesName })
    });
    if (r1.status !== 'success') throw new Error(r1.message || '\u5931\u8d25');
    var count = r1.count || 0;
    if (count === 0) {
      /* 该库没有文件：常见于 Emby / 缓存里残留了失效条目。提供「同步状态」，不删任何文件 */
      if (confirm('该库没有此剧文件。\n\n是否按磁盘实际情况同步状态？\n（不会删除任何文件；已无文件的剧会从列表移除）')) {
        var rs = await api('/api/emby/series/' + seriesId + '/resync', { method: 'POST', body: '{}' });
        if (rs.status !== 'success') throw new Error(rs.message || '失败');
        applySeriesDeleteResult(seriesId, rs);
        closeModal();
        toast('已同步状态', 'success');
      }
      return;
    }

    // 2. 原\u751f confirm \u4e8c\u6b21\u786e\u8ba4
    var msg = '\u26a0\ufe0f \u786e\u8ba4\u5220\u9664\u3010' + targetName + '\u3011\uff1f\n\n' +
              '\u5267\u540d\uff1a' + (seriesName || '') + '\n' +
              '\u5c06\u5220\u9664 ' + count + ' \u4e2a\u6587\u4ef6' +
              (target === 'local' ? '\uff0c\u5e76\u8054\u52a8\u5220\u9664 115 \u7f51\u76d8\u4e0a\u7684\u6e90\u6587\u4ef6' : '') +
              '\n\n\u6b64\u64cd\u4f5c\u4e0d\u53ef\u6062\u590d\uff01';
    if (!confirm(msg)) return;

    // 3. 真\u5220
    var r2 = await api('/api/emby/series/' + seriesId + '/delete', {
      method: 'POST',
      body: JSON.stringify({ target: target, dry_run: false, series_name: seriesName })
    });
    if (r2.status !== 'success') throw new Error(r2.message || '\u5931\u8d25');

    /* 先立即更新界面（海报马上撤下），再提示结果；Emby 那边由服务端后台通知 */
    applySeriesDeleteResult(seriesId, r2);
    closeModal();
    var partial = (r2.errors && r2.errors.length) || (r2.strm_removed || 0) < count;
    toast('\u2705 \u5df2\u5220\u9664 strm ' + (r2.strm_removed || 0) + ' \u4e2a' +
          (target === 'local' ? '\uff0c\u4e91\u7aef ' + (r2.cloud_removed || 0) + ' \u4e2a' : ''), 'success', 3500);
    if (partial) {
      setTimeout(function(){
        alert('\u26a0\ufe0f \u90e8\u5206\u6587\u4ef6\u672a\u5220\u9664\uff08\u4fdd\u62a4\u673a\u5236\uff1a\u4e91\u7aef\u6e90\u6587\u4ef6\u627e\u4e0d\u5230/\u5339\u914d\u4e0d\u552f\u4e00\u65f6\u4f1a\u4fdd\u7559 strm\uff09\uff1a\n' +
              (r2.errors || []).slice(0, 3).join('\n'));
      }, 50);
    }
  } catch(e){
    alert('\u5220\u9664\u5931\u8d25\uff1a' + e.message);
  }
}

/* 只重新拉取单部剧集在 Emby 中的最新分集情况，替换/移除列表里对应的一条，
   避免像整页刷新那样把所有剧集的 TMDB 对照状态打回"待对照"。 */
function applySeriesDeleteResult(seriesId, r){
  var idx = embyData.series.findIndex(function(x){ return x.id === seriesId; });
  if (r.series_removed) {
    if (idx >= 0) embyData.series.splice(idx, 1);
    delete window.__embySeriesMap[seriesId];
  } else if (r.entry && idx >= 0) {
    var md = embyData.series[idx]._md;
    if (md) r.entry._md = md;
    embyData.series[idx] = r.entry;
    window.__embySeriesMap[seriesId] = r.entry;
  }
  embyData.stats.total_series = embyData.series.length;
  $('emby-total').textContent = embyData.stats.total_series || 0;
  renderEmbyList();
}

async function refreshSingleSeriesInList(seriesId, deletedTarget){
  try {
    /* 服务端已按磁盘真实文件修补了缓存：直接读回该剧最新条目（缓存只读，不触发 TMDB 对照） */
    var r = await api('/api/emby/library?force=0&with_tmdb=1&cache_only=1');
    var fresh = (r && r.series) ? r.series.find(function(x){ return x.id === seriesId; }) : null;
    var idx = embyData.series.findIndex(function(x){ return x.id === seriesId; });
    if (!fresh) {
      if (idx >= 0) embyData.series.splice(idx, 1);
      delete window.__embySeriesMap[seriesId];
    } else if (idx >= 0) {
      var md = embyData.series[idx]._md;
      embyData.series[idx] = fresh;
      if (md) fresh._md = md;
      window.__embySeriesMap[seriesId] = fresh;
    }
    embyData.stats.total_series = embyData.series.length;
    $('emby-total').textContent = embyData.stats.total_series || 0;
    renderEmbyList();
  } catch(e) {
    // 局部刷新失败不影响已完成的删除操作，静默忽略
  }
}

function loadLibBreakdown(series, ti){
  var box = $('libBreakdown');
  if (!box) return;
  var seasons = series.seasons || [];
  var tmdbMap = {};
  (ti.seasons || []).forEach(function(se){ tmdbMap[se.season] = se.tmdb; });
  // 旧快照没有每季分库统计时如实说明；不现查另一批分集冒充当前版本。
  var known = seasons.every(function(se){ return se.local_eps != null && se.share_eps != null; });
  if (!known) {
    box.innerHTML = '<div>本地 ' + (series.local_eps || 0) + ' 集 · 分享 ' + (series.share_eps || 0)
      + ' 集</div><div style="margin-top:6px">当前缓存尚无逐季分库明细，下一次片库更新后可查看。</div>';
    return;
  }
  var bySeason = {};
  seasons.forEach(function(se){ bySeason[se.season] = se; });
  var allSeasons = Array.from(new Set(Object.keys(bySeason).concat(Object.keys(tmdbMap)))).map(Number).sort(function(a,b){return a-b;});
  if (!allSeasons.length) { box.innerHTML = '<span>无分集数据</span>'; return; }
  function libraryCell(have, total, name, tone){
    var kind = 'unknown', label = '待对照';
    if (total != null) {
      var diff = have - total;
      kind = diff === 0 ? 'complete' : (diff < 0 ? 'missing' : 'extra');
      label = diff === 0 ? '✓ 完整' : (diff < 0 ? '缺 ' + Math.abs(diff) + ' 集' : '超 ' + diff + ' 集');
    }
    var pct = total > 0 ? Math.max(0, Math.min(100, have / total * 100)) : (total === 0 && have === 0 ? 100 : 0);
    return '<div class="lib-season-library ' + tone + '"><div class="lib-season-library-heading"><span>' + name + '</span><b class="lib-season-count">' + esc(have) + '<small> 集</small></b></div>'
      + '<div class="lib-season-progress" aria-hidden="true"><i style="width:' + pct + '%"></i></div>'
      + '<span class="lib-season-status ' + kind + '">' + esc(label) + '</span></div>';
  }
  var html = '<div class="lib-breakdown-heading"><h4>两库分集</h4><span>' + allSeasons.length + ' 季 · TMDB 已播对照</span></div><div class="lib-season-list">';
  allSeasons.forEach(function(sn){
    var se = bySeason[sn] || {local_eps:0,share_eps:0};
    var total = tmdbMap[sn];
    html += '<section class="lib-season-card" aria-label="' + (sn === 0 ? '特别篇' : '第 ' + sn + ' 季') + '">'
      + '<div class="lib-season-heading"><b>' + (sn === 0 ? '特别篇' : 'S' + String(sn).padStart(2,'0')) + '</b><span>已播 <strong>' + (total != null ? esc(total) : '—') + '</strong> 集</span></div>'
      + libraryCell(se.local_eps, total, '本地库', 'local') + libraryCell(se.share_eps, total, '分享库', 'share') + '</section>';
  });
  box.innerHTML = html + '</div>';

}

function renderMovieCard(m){
  var libTag = '';
  if (m.in_local && m.in_share) libTag = '双库';
  else if (m.in_local) libTag = '本地';
  else if (m.in_share) libTag = '分享';

  /* TMDB poster_path 优先（上游海报对照逻辑），Emby 缓存图兜底 */
  var mtp = m.poster_tmdb ? '/api/tmdb/poster' + m.poster_tmdb : '';
  var poster = mtp
    ? '<div class="poster-loading">海报加载中</div><img data-emby-src="' + esc(mtp) + '" loading="lazy" decoding="async" onload="clearPosterLoading(this)" onerror="clearPosterLoading(this);this.onerror=null;this.style.display=\'none\';var n=this.nextElementSibling;if(n)n.style.display=\'flex\';"><div class="no-img" style="display:none">暂无海报</div>'
    : (m.has_image
      ? '<div class="poster-loading">海报加载中</div><img data-emby-src="/api/emby/poster/' + esc(m.id) + '" loading="lazy" decoding="async" onload="clearPosterLoading(this)">'
      : '<div class="no-img">暂无海报</div>');

  return '<div class="poster-card emby-poster-card" onclick="showMovieDetailById(' + jsarg(m.id) + ')">'
    + '<div class="poster-wrap">' + poster
    + '<span class="badge-type">电影</span>'
    + '</div>'
    + '<div class="info">'
    + '<div class="t" title="' + esc(m.name || '?') + '">' + esc(m.name || '?') + '</div>'
    + '<div class="meta"><span>' + (m.year || '—') + ' · ' + libTag + '</span></div>'
    + '</div>'
    + '</div>';
}

