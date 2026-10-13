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
  if (mappingPagedActive) {
    // Manual completion changes status filter membership: use current server facts.
    await loadMappingPaged(true);
    if ((window.__embySeriesMap || {})[id]) showEmbyDetailById(id);
    toast(on ? '已标记完结，不再计入缺集' : '已取消完结，恢复 TMDB 对照结果', 'success', 2600);
    if (typeof loadDashEmby === 'function') loadDashEmby();
    return;
  }
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
  if (mappingPagedActive || mappingPagedLoading || !embyLoaded) {
    name = catClean(name);
    embyFilterState.cat = name;
    embyFilterState.catMode = 'seg';
    embyFilterState.filter = 'all';
    embyFilterState.scope = (scope === 'local' || scope === 'share') ? scope : 'all';
    embyFilterState.type = /电影/.test(name) ? 'movies' : 'series';
    $('embySearch').value = '';
    var wanted = name;
    switchTab('mapping');
    setEmbyType(embyFilterState.type);
    var generation = mappingPagedGeneration;
    document.querySelectorAll('#embyFilter button').forEach(function(b){b.classList.toggle('active',b.dataset.f==='all');});
    document.querySelectorAll('#embyScopeFilter button').forEach(function(b){b.classList.toggle('active',b.dataset.s===embyFilterState.scope);});
    Promise.all([api(mappingPagedQuery(0,'series')),api(mappingPagedQuery(0,'movies'))]).then(function(results){
      if (generation !== mappingPagedGeneration || embyFilterState.cat !== wanted || window.__activeTab !== 'mapping') return;
      var seriesCount = results[0].status === 'success' ? results[0].total : 0;
      var movieCount = results[1].status === 'success' ? results[1].total : 0;
      var desired = !seriesCount && movieCount ? 'movies' : 'series';
      if (embyFilterState.type !== desired) setEmbyType(desired);
    }).catch(function(){});
    return;
  }
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
var __mappingPreviewPending = null;
/* P5: server-side filtered, globally sorted Mapping pages. */
var mappingPagedActive = false;
var mappingPagedLoading = false;
var mappingPagedGeneration = 0;
var mappingPagedController = null;
var mappingPagedTotal = 0;
var mappingPagedOffset = 0;
var mappingPagedVersion = null;
var mappingPagedDirty = false;
var mappingPagedDetails = Object.create(null);
var mappingPagedDetailRequests = Object.create(null);
var mappingPagedSearchTimer = null;
function scheduleMappingSearch(){
  if (mappingPagedSearchTimer) clearTimeout(mappingPagedSearchTimer);
  mappingPagedSearchTimer = setTimeout(function(){mappingPagedSearchTimer=null;embyPage=1;renderEmbyList();}, 220);
}
function mappingPagedQuery(offset, mediaOverride){
  var args = [
    ['media', mediaOverride || embyFilterState.type],
    ['status', embyFilterState.filter || 'all'],
    ['scope', embyFilterState.scope || 'all'],
    ['category', embyFilterState.cat || ''],
    ['category_mode', embyFilterState.catMode === 'sub' ? 'sub' : 'seg'],
    ['keyword', ($('embySearch').value || '').trim()],
    ['offset', offset], ['limit', 40]
  ];
  return '/api/emby/library/page?' + args.map(function(x){return encodeURIComponent(x[0]) + '=' + encodeURIComponent(x[1]);}).join('&');
}
function mappingPagedStats(stats){
  embyData.stats = stats || {};
  embyData.statsBase = stats || {};
  var st = embyData.stats;
  ['embyStats','embyStats2','embyTitle1','embyTitle2'].forEach(function(id){var el=$(id);if(el)el.style.display='';});
  var counts = {'emby-title-count':st.total_series,'emby-title-movies':st.total_movies,
    'emby-total':st.total_series,'emby-aligned':st.aligned,'emby-missing':st.missing,
    'emby-extra':st.extra,'emby-ongoing':st.ongoing,
    'emby-unmatched':(st.unmatched||0)+(st.no_tmdb||0),'emby-movies':st.total_movies};
  Object.keys(counts).forEach(function(id){var node=$(id);if(node)node.textContent=counts[id]||0;});
  updateEmbyChips();
}
async function loadMappingPaged(reset){
  if (typeof mappingBatchSyncContext === 'function') mappingBatchSyncContext();
  reset = reset !== false;
  if (!reset && (mappingPagedLoading || mappingPagedOffset >= mappingPagedTotal)) return;
  var generation = ++mappingPagedGeneration;
  if (mappingPagedController) mappingPagedController.abort();
  var ctl = typeof AbortController === 'function' ? new AbortController() : null;
  mappingPagedController = ctl;
  mappingPagedLoading = true;
  if (mappingPagedActive) updateEmbyLoadMore(mappingPagedTotal);
  var offset = reset ? 0 : mappingPagedOffset;
  if (reset && !embyLoaded) $('embyList').innerHTML = '<div class="list-empty">正在加载片库首屏…</div>';
  var requestedMedia = embyFilterState.type;
  try {
    var r = await api(mappingPagedQuery(offset), {timeoutMs:12000, signal:ctl ? ctl.signal : undefined});
    if (generation !== mappingPagedGeneration) return;
    if (!r || r.status !== 'success') {
      if (r && r.status === 'nocache' && !mappingPagedActive) {
        // A genuinely cold first installation keeps the original Emby fallback.
        return loadEmbyLibrary(false, false);
      }
      throw new Error((r && r.message) || '分页接口暂不可用');
    }
    if (!reset && mappingPagedVersion !== r.facts_version) {
      toast('片库数据已更新，正在重新显示第一页', '', 2200);
      return loadMappingPaged(true);
    }
    if (reset) {
      mappingPagedDetails = Object.create(null);
      mappingPagedDetailRequests = Object.create(null);
      embyData.series = [];
      embyData.movies = [];
      window.__embySeriesMap = {};
      window.__embyMovieMap = {};
      embyShown = 0;
      mappingPrefetch = null;
      mappingPagedVersion = r.facts_version;
    }
    var rows = r.rows || [];
    var list = $('embyList');
    var kind = requestedMedia === 'movies' ? 'movies' : 'series';
    var fn = kind === 'movies' ? renderMovieCard : renderSeriesCard;
    var arr = kind === 'movies' ? embyData.movies : embyData.series;
    var html = rows.map(fn).join('');
    if (reset) {
      if (rows.length) replaceMappingCardsStable(list, rows, fn, kind);
      else list.innerHTML = '<div class="list-empty">无匹配项</div>';
      if (list.dataset) delete list.dataset.preview;
    } else list.insertAdjacentHTML('beforeend', html);
    rows.forEach(function(item){
      arr.push(item);
      if (item.id) (kind === 'movies' ? window.__embyMovieMap : window.__embySeriesMap)[item.id] = item;
    });
    embyView = {arr:arr, fn:fn};
    embyPage = 1;
    embyShown = arr.length;
    embyLoaded = true;
    mappingPagedActive = true;
    mappingPagedDirty = false;
    mappingPagedOffset = r.next_offset;
    mappingPagedTotal = r.total;
    if (r.emby_host) window.__embyHost = r.emby_host;
    if (r.manual_done && typeof r.manual_done === 'object') window.__mdSet = r.manual_done;
    mappingPagedStats(r.stats);
    hydratePosters(list);
    updateEmbyLoadMore(mappingPagedTotal);
    schedulePosterAutoLoad();
  } catch(e) {
    if (generation !== mappingPagedGeneration) return;
    // Older deployments have no P5 endpoint. Preserve the old full-load path.
    if (!mappingPagedActive && /HTTP 404|404 Not Found|^Not Found$/.test(e.message || '')) {
      mappingPagedLoading = false;
      return loadEmbyLibrary(false, true, true);
    }
    if (mappingPagedActive) toast('片库分页加载失败，可重试：' + e.message, 'error', 4500);
    else $('embyList').innerHTML = '<div class="list-empty">' + esc(e.message) +
      '<br><button class="btn btn-ghost" onclick="loadMappingPaged(true)">重新加载</button></div>';
  } finally {
    if (generation === mappingPagedGeneration) {
      mappingPagedLoading = false;
      if (mappingPagedController === ctl) mappingPagedController = null;
      if (mappingPagedActive) updateEmbyLoadMore(mappingPagedTotal);
    }
  }
}
function mappingNeedDetail(media, id){
  if (!mappingPagedActive) return false;
  var key = media + ':' + id;
  if (mappingPagedDetails[key] === mappingPagedVersion) return false;
  if (mappingPagedDetailRequests[key]) return true;
  var version = mappingPagedVersion, generation = mappingPagedGeneration;
  var pending = api('/api/emby/library/item?media=' + encodeURIComponent(media) + '&id=' + encodeURIComponent(id), {timeoutMs:12000})
    .then(function(r){
      if (generation !== mappingPagedGeneration) return;
      if (r.status !== 'success' || !r.item) throw Error(r.message || '作品已不存在，请刷新片库');
      if (version !== mappingPagedVersion || r.facts_version !== version) {loadMappingPaged(true);return;}
      mappingPagedDetails[key] = version;
      if (media === 'series') {
        window.__embySeriesMap[id] = r.item;
        if (mappingDetailVisible(key)) showEmbyDetailById(id);
      } else {
        window.__embyMovieMap[id] = r.item;
        if (mappingDetailVisible(key)) showMovieDetailById(id);
      }
    }).catch(function(e){if (generation === mappingPagedGeneration) toast('读取作品详情失败：' + e.message, 'error', 4000);})
    .finally(function(){if (mappingPagedDetailRequests[key] === pending) delete mappingPagedDetailRequests[key];});
  mappingPagedDetailRequests[key] = pending;
  return true;
}
function mappingDetailVisible(key){
  var body = $('modalBody'), bg = $('modalBg');
  return !!(body && bg && bg.classList.contains('show') && body.getAttribute('data-mapping-detail') === key);
}
function mappingDetailPending(media, id, pending){
  var body = $('modalBody');
  body.setAttribute('data-mapping-detail', media + ':' + id);
  if (!pending) return;
  body.querySelectorAll('.mapping-detail-actions button:not(.mapping-action-close)').forEach(function(b){b.disabled = true;});
  body.insertAdjacentHTML('beforeend', '<p class="hint" role="status">正在补充分季详情，可先查看作品信息…</p>');
}

function mappingPreviewAllowed(){
  return !embyLoaded && embyFilterState.type === 'series' &&
    embyFilterState.filter === 'all' && embyFilterState.scope === 'all' &&
    !embyFilterState.cat && !(($('embySearch').value || '').trim());
}
async function showMappingFirstScreen(generation){
  // This is only a bounded snapshot from the same server facts as the full API.
  // Never install a partial array in embyData or unlock delete/detail actions.
  var controller = typeof AbortController === 'function' ? new AbortController() : null;
  __mappingPreviewPending = controller;
  try {
    var response = await api('/api/emby/library/preview', {
      timeoutMs: 6500, signal: controller ? controller.signal : undefined
    });
    if (generation !== embyLibraryGeneration || embyLoaded || !mappingPreviewAllowed() ||
        !response || response.status !== 'success' || !Array.isArray(response.series)) return;
    var list = $('embyList');
    var cards = response.series.slice(0, posterNextCount(0, list));
    if (!cards.length) return;
    list.innerHTML = cards.map(renderSeriesCard).join('');
    if (list.dataset) list.dataset.preview = '1';
    var hint = $('embyInfText');
    if (hint) hint.textContent = '先显示 ' + cards.length + ' 张缓存海报 · 完整片库同步中…';
    hydratePosters(list);
  } catch(e) { /* The full request remains the fallback. */ }
  finally { if (__mappingPreviewPending === controller) __mappingPreviewPending = null; }
}
// The full response must not remount unchanged poster nodes after the preview.
function replaceMappingCardsStable(list, rows, render, kind){
  var html = rows.map(render).join('');
  if (typeof document !== 'undefined' && typeof list.querySelectorAll === 'function') {
    var old = list.querySelectorAll('.poster-card');
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var fresh = tmp.querySelectorAll('.poster-card');
    if (old.length && fresh.length === rows.length) {
      var byId = Object.create(null);
      var attr = kind === 'series' ? 'data-series-id' : 'data-movie-id';
      old.forEach(function(card){ byId[card.getAttribute(attr)] = card; });
      var fragment = document.createDocumentFragment();
      for (var i=0; i<fresh.length; i++) {
        var previous = byId[fresh[i].getAttribute(attr)];
        var a = previous && previous.querySelector('img');
        var b = fresh[i].querySelector('img');
        var sameImage = (!a && !b) || (a && b && a.getAttribute('data-emby-src') === b.getAttribute('data-emby-src'));
        if (previous && sameImage) {
          // Move original node, including its decoded image, into new order.
          var oldInfo = previous.querySelector('.info'), newInfo = fresh[i].querySelector('.info');
          if (oldInfo && newInfo && oldInfo.innerHTML !== newInfo.innerHTML) oldInfo.innerHTML = newInfo.innerHTML;
          var oldBadge = previous.querySelector('.badge-status'), newBadge = fresh[i].querySelector('.badge-status');
          if (oldBadge && newBadge) {
            oldBadge.className = newBadge.className;
            oldBadge.textContent = newBadge.textContent;
          }
          var mark = previous.querySelector('.mapping-batch-card-check');
          var nextMark = fresh[i].querySelector('.mapping-batch-card-check');
          if (mark) mark.remove();
          if (nextMark) previous.appendChild(nextMark);
          previous.className = fresh[i].className;
          fragment.appendChild(previous);
        } else fragment.appendChild(fresh[i]);
      }
      list.replaceChildren(fragment);
      delete list.dataset.preview;
      return;
    }
  }
  list.innerHTML = html;
  if (list.dataset) delete list.dataset.preview;
}

var embyLibraryLoading = false, embyLibraryGeneration = 0;
function ensureEmbyLoaded(){
  if (mappingPagedActive && mappingPagedDirty && !mappingPagedLoading) {
    mappingPagedDirty = false;
    return loadMappingPaged(true);
  }
  if (!embyLoaded && !embyLibraryLoading && !mappingPagedLoading) loadMappingPaged(true);
}
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
  if (mappingPagedActive && !force && withTmdb) return loadMappingPaged(true);
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
  if (__mappingPreviewPending) { __mappingPreviewPending.abort(); __mappingPreviewPending = null; }
  if (!embyLoaded) $('embyList').innerHTML = '<div class="list-empty">' + msg + '</div>';
  if (!embyLoaded && !force && cacheOnly && mappingPreviewAllowed()) {
    // Fire independently: a slow/no-cache preview must never delay full data.
    showMappingFirstScreen(generation);
  }
  try {
    var url = '/api/emby/library?force=' + (force ? 1 : 0) + '&with_tmdb=' + (withTmdb ? 1 : 0) + (cacheOnly ? '&cache_only=1' : '') + '&web=1';
    var r = await api(url);
    if (generation !== embyLibraryGeneration) return;
    // 没有对照缓存（首次使用 / 缓存太旧）：退回快速模式，只拉 Emby 库，不触发 TMDB 对照
    if (cacheOnly && r.status === 'nocache') return loadEmbyLibrary(false, false);
    if (r.status !== 'success') throw new Error(r.message || '失败');
    if (cacheOnly && r.ts) toast('已载入片库缓存（' + fmtAgo(r.ts) + '对照），需要最新数据请点「对照 TMDB」', 'success', 3200);
    // Web responses include authoritative manual marks, so cached cards need one request.
    if (r.manual_done && typeof r.manual_done === 'object') {
      window.__mdSet = r.manual_done;
      var legacyManual = null;
      try { legacyManual = localStorage.getItem('manualDone'); } catch(e){}
      if (legacyManual) await mdLoad(); // Preserve one-time migration from old browsers.
    } else {
      await mdLoad(); // Legacy/basic response compatibility.
    }
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
  if (mappingPagedActive) return loadMappingPaged(false);
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
    var loading = embyMoreLoading || (mappingPagedActive && mappingPagedLoading);
    button.disabled = loading;
    button.textContent = loading ? '加载中...' : (embyMoreFailed ? '重试加载下一批' : '加载更多');
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
// Sort the authoritative series collection once per revision. Subsequent
// category/search/scope filters keep that identical ordering without O(n log n)
// work for each interaction. The live-delta handler invalidates this cache.
var __mappingSortedCache = {source:null, rows:null};
function sortedMappingSeries(){
  if (__mappingSortedCache.source === embyData.series && __mappingSortedCache.rows &&
      __mappingSortedCache.rows.length === embyData.series.length)
    return __mappingSortedCache.rows;
  var prio = { missing: 0, extra: 1, unmatched: 2, no_tmdb: 2, ongoing: 3, aligned: 4 };
  var rows = embyData.series.slice();
  rows.sort(function(a, b){
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
  __mappingSortedCache = {source:embyData.series, rows:rows};
  return rows;
}
function renderEmbyList(){
  if (typeof mappingBatchSyncContext === 'function') mappingBatchSyncContext();
  if (mappingPagedActive || mappingPagedLoading) {loadMappingPaged(true);return;}
  var list = $('embyList');
  if (!embyLoaded) {
    // Filters/search are not applied to a partial preview as if it were complete.
    list.innerHTML = '<div class="list-empty">正在读取完整片库，筛选稍后可用…</div>';
    if (list.dataset) delete list.dataset.preview;
    return;
  }
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
    replaceMappingCardsStable(list, arr.slice(0, embyShown), renderMovieCard, 'movies');
    hydratePosters(list);
    updateEmbyLoadMore(arr.length);
    schedulePosterAutoLoad();
    return;
  }
  arr = sortedMappingSeries().slice();
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
  embyView = { arr: arr, fn: renderSeriesCard };
  embyShown = embyPage === 1 ? Math.min(arr.length, posterNextCount(0, list)) : Math.min(arr.length, embyShown);
  replaceMappingCardsStable(list, arr.slice(0, embyShown), renderSeriesCard, 'series');
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

/* 电视剧批量清理：明确选择单个目标库；不自动删除。 */
var mappingBatchMode = false;
var mappingBatchAll = false;
var mappingBatchSelected = new Set();
var mappingBatchContext = '';
function mappingBatchAllowed(){
  return (embyFilterState.type === 'series' || embyFilterState.type === 'movies') &&
    (embyFilterState.scope === 'local' || embyFilterState.scope === 'share');
}
function mappingBatchFilters(){
  return {target:embyFilterState.scope, scope:embyFilterState.scope,
    filter:embyFilterState.filter || 'all', category:embyFilterState.cat || '',
    category_mode:embyFilterState.catMode === 'sub' ? 'sub' : 'seg',
    keyword:($('embySearch').value || '').trim()};
}
function mappingBatchSyncContext(){
  var sig = JSON.stringify([embyFilterState.type, embyFilterState.filter,
    embyFilterState.scope, embyFilterState.cat, embyFilterState.catMode,
    ($('embySearch').value || '').trim()]);
  if (sig !== mappingBatchContext) {
    mappingBatchContext = sig;
    mappingBatchMode = false;
    mappingBatchAll = false;
    mappingBatchSelected.clear();
  }
  var permitted = mappingBatchAllowed();
  var entry = $('mappingBatchEntry'), panel = $('mappingBatchPanel');
  if (entry) entry.hidden = !permitted;
  if (panel) panel.hidden = !permitted || !mappingBatchMode;
  var isLocal = embyFilterState.scope === 'local';
  var entryHint = $('mappingBatchEntryHint');
  if (entryHint) entryHint.textContent = isLocal ? '本地库 · 联动匹配源文件' : '分享库 · 仅 STRM';
  var hint = $('mappingBatchHint');
  if (hint) hint.textContent = isLocal
    ? '删除本地 STRM 和匹配的源视频；每次最多 20 部。'
    : '仅清理分享库 STRM，不会删除源视频；每次最多 60 部。';
  var count = $('mappingBatchCount');
  if (count) count.textContent = mappingBatchAll
    ? '已全选当前筛选结果（包括未加载页面）'
    : '已选 ' + mappingBatchSelected.size + ' 部';
  var selected = $('mappingBatchDeleteSelected');
  if (selected) selected.disabled = (!mappingBatchAll && !mappingBatchSelected.size) || mappingDeleteBusy;
  var allButton = $('mappingBatchSelectAll');
  if (allButton) {
    allButton.textContent = mappingBatchAll ? '取消全选' : '全选筛选结果';
    allButton.disabled = mappingDeleteBusy;
    allButton.setAttribute('aria-pressed', mappingBatchAll ? 'true' : 'false');
  }
  var start = $('mappingBatchStart');
  if (start) start.textContent = mappingBatchMode ? '退出多选' : '多选清理';
}
function mappingBatchToggleMode(){
  if (!mappingBatchAllowed() || mappingDeleteBusy) return;
  mappingBatchMode = !mappingBatchMode;
  mappingBatchAll = false;
  mappingBatchSelected.clear();
  mappingBatchSyncContext();
  renderEmbyList();
}
// 全选只改变选择状态，不预览、更不提交删除。跨页范围由后端在预览时核定。
function mappingBatchSelectFiltered(){
  if (!mappingBatchAllowed() || !mappingBatchMode || mappingDeleteBusy) return;
  mappingBatchAll = !mappingBatchAll;
  mappingBatchSelected.clear();
  document.querySelectorAll('#embyList .mapping-batch-card-check input').forEach(function(node){
    node.checked = mappingBatchAll;
    node.disabled = mappingBatchAll;
  });
  mappingBatchSyncContext();
}
function mappingBatchSetItem(id, checked){
  if (!mappingBatchAllowed() || !mappingBatchMode || mappingBatchAll || mappingDeleteBusy) return;
  if (checked) mappingBatchSelected.add(String(id));
  else mappingBatchSelected.delete(String(id));
  mappingBatchSyncContext();
}
function mappingBatchProgress(preview){
  var isLocal = preview.target === 'local';
  mappingDeleteView = {target:preview.target, title:'批量清理' + (isLocal?'本地库':'分享库'),
    count:preview.count, kind:'batch', id:preview.preview_id};
  mappingDeletePhase('正在逐部重新验证并执行安全删除…');
  openModal('<div class="mapping-detail"><h3>批量删除进行中</h3>'
    + '<p>目标：' + (isLocal?'本地库（可能联动删除源视频）':'分享库（仅 STRM）') + '</p>'
    + '<p>已确认 ' + esc(preview.count) + ' 部 / ' + esc(preview.strm_count) + ' 个 STRM；执行中会再次核验。</p>'
    + '<p id="mappingDeleteStatus" role="status" aria-live="polite"></p>'
    + '<progress id="mappingDeleteBar" value="0" max="' + esc(preview.count) + '" style="width:100%"></progress>'
    + '<p id="mappingDeleteCounts" role="status" aria-live="polite">等待后端反馈…</p>'
    + '<p id="mappingDeleteFile" style="overflow-wrap:anywhere;font-size:12px"></p>'
    + '<button class="btn gray" onclick="closeModal()">关闭提示（删除继续执行）</button></div>');
  renderMappingDeleteState();
}
async function mappingBatchDelete(){
  if (!mappingBatchAllowed() || !mappingBatchMode ||
      (!mappingBatchAll && !mappingBatchSelected.size) || !beginMappingDelete()) return;
  var phase = 'preview';
  try {
    var selection = mappingBatchAll ? 'all' : 'selected';
    var body = Object.assign(mappingBatchFilters(), {selection:selection});
    var media=embyFilterState.type === 'movies' ? 'movies' : 'series';
    if (selection === 'selected') body[media==='movies'?'movie_ids':'series_ids'] = Array.from(mappingBatchSelected);
    var preview = await api('/api/emby/'+media+'/batch-delete/preview', {
      method:'POST',body:JSON.stringify(body),timeoutMs:60000});
    if (preview.status !== 'success') throw new Error(preview.message || '预览失败');
    var isLocal = preview.target === 'local';
    var display = (preview.series || []).slice(0,12).map(function(x){return '· ' + x.name + '（' + x.count + ' STRM）';}).join('\n');
    var label = isLocal ? '本地库' : '分享库';
    var danger = isLocal ? '\n⚠️ 本地库将按原有治理规则联动删除对应的 CD2 源视频！' : '\n分享库只删除 NAS STRM，不删除源视频。';
    var summary = '确定清理【' + label + '】吗？\n筛选：' + (body.filter||'all') +
      ' · 共 ' + preview.count + ' 部 / ' + preview.strm_count + ' 个 STRM\n' +
      display + (preview.count>12?'\n…其余作品略':'') + danger +
      '\n其他程序后续的同步或文件生成行为不受本次清理控制。\n此操作不可撤销。';
    if (!confirm(summary)) return;
    var phrase = isLocal ? '删除本地库及源文件' : '删除分享库';
    if (prompt('再次确认：请完整输入「' + phrase + '」', '') !== phrase) {
      toast('取消删除：确认文字不符', '', 3500); return;
    }
    phase = 'delete';
    mappingBatchProgress(preview);
    var result = await executeMappingDelete('/api/emby/'+media+'/batch-delete/execute', {
      target:preview.target,preview_id:preview.preview_id,confirmation:phrase,confirm:true});
    if (result.status !== 'success') throw new Error(result.message || '批量执行未完成');
    mappingBatchSelected.clear();mappingBatchAll=false;mappingBatchMode=false;
    var detail = '已处理 ' + (result.processed||0) + '/' + preview.count + ' 部；'
      + ' STRM 已删除 ' + (result.strm_removed||0) + ' 个；'
      + (isLocal?'源视频已删除 ' + (result.cloud_removed||0) + ' 个；':'')
      + '保留 ' + (result.retained||0) + ' 个；失败/部分完成 ' + (result.failed||0) + ' 部。';
    if ((result.errors||[]).length) detail += '\n' + result.errors.slice(0,10).join('\n');
    showMappingDeleteResult(result.failed?'批量清理部分完成':'批量清理完成', detail,
      '请等待 Emby 更新后重新核对片库；勿根据旧页面重复删除。');
    // 完整删除结果只在模态框呈现一次；不再重复显示 Toast。
    if (mappingPagedActive) await loadMappingPaged(true);
    else await loadEmbyLibrary(false, true, true);
  } catch(err) {
    showMappingDeleteResult('批量清理状态',
      phase==='delete'?'结果尚未确认，可能已有文件删除；不要重复提交，请检查日志和片库。':'尚未执行删除。',
      err.message||String(err));
  } finally {
    finishMappingDelete(); mappingBatchSyncContext();
  }
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

  var mark = (typeof mappingBatchMode !== 'undefined' && mappingBatchMode && typeof mappingBatchAllowed === 'function' && mappingBatchAllowed()) ?
    '<label class="mapping-batch-card-check" title="选择此剧" onclick="event.stopPropagation()">'
    + '<input type="checkbox" aria-label="选择 ' + esc(s.name || '剧集') + '" '
    + (mappingBatchAll ? 'checked disabled ' : (mappingBatchSelected.has(String(s.id)) ? 'checked ' : ''))
    + 'onchange="mappingBatchSetItem(' + jsarg(s.id) + ',this.checked)"></label>' : '';
  return '<div class="poster-card emby-poster-card st-' + mst + '" data-series-id="' + esc(s.id) + '" onclick="showEmbyDetailById(' + jsarg(s.id) + ')">'
    + mark + '<div class="poster-wrap">' + poster
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

/* 同一页面只允许一个删除预览/请求；关闭或重开详情不解除正在执行的请求。 */
var mappingDeleteBusy = false;
var mappingDeleteMessage = '';
var mappingDeleteView = null;
function renderMappingDeleteState(){
  document.querySelectorAll('[data-mapping-delete]').forEach(function(button){
    button.disabled = mappingDeleteBusy;
    button.setAttribute('aria-busy', String(mappingDeleteBusy));
  });
  var status = $('mappingDeleteStatus');
  if (status) {
    status.textContent = mappingDeleteBusy ? mappingDeleteMessage : '';
    status.hidden = !mappingDeleteBusy;
  }
}
function beginMappingDelete(){
  if (mappingDeleteBusy) {
    toast('删除正在处理中，请等待结果，不要重复提交', '', 4000);
    return false;
  }
  mappingDeleteBusy = true;
  mappingDeleteMessage = '正在核对文件，请稍候…';
  renderMappingDeleteState();
  return true;
}
function mappingDeletePhase(message){
  mappingDeleteMessage = message;
  renderMappingDeleteState();
}
function mappingDeletePoster(entry, kind){
  if (!entry) return '';
  var poster = kind === 'series' ? (entry.tmdb_info || {}).poster : entry.poster_tmdb;
  var url = poster ? '/api/tmdb/poster' + poster : (entry.has_image ? '/api/emby/poster/' + encodeURIComponent(entry.id) : '');
  return url ? '<img data-emby-src="' + esc(url) + '" style="width:72px;height:108px;object-fit:cover;border-radius:8px;flex-shrink:0">' : '';
}
function mappingDeleteEntry(){
  if (!mappingDeleteView) return null;
  var view = mappingDeleteView;
  var list = view.kind === 'series' ? embyData.series : embyData.movies;
  return list.find(function(x){return view.kind === 'series' ? String(x.id) === String(view.id) : String(x.tmdb_id || '') === String(view.id);}) || null;
}
function mappingDeleteHeader(entry){
  var view = mappingDeleteView || {};
  var facts = '';
  if (entry) {
    if (entry.in_local) facts += '本地库' + (view.kind === 'series' ? ' ' + (entry.local_eps || 0) + ' 集' : '');
    if (entry.in_share) facts += (facts ? ' · ' : '') + '分享库' + (view.kind === 'series' ? ' ' + (entry.share_eps || 0) + ' 集' : '');
  }
  return '<div style="display:flex;gap:14px;align-items:center;margin-bottom:16px">'
    + mappingDeletePoster(entry, view.kind) + '<div><strong>' + esc(view.title || '所选作品')
    + '</strong>' + (facts ? '<p>' + esc(facts) + '</p>' : '') + '</div></div>';
}
function showMappingDeleteProgress(target, title, count, kind, id){
  mappingDeleteView = {target:target, title:title, count:count, kind:kind, id:id};
  mappingDeletePhase(target === 'local' ? '正在删除本地 STRM 及 115 源文件…' : '正在删除分享库 STRM…');
  openModal('<div class="mapping-detail"><h3 style="margin:0 0 16px">正在删除</h3>'
    + mappingDeleteHeader(mappingDeleteEntry())
    + '<p>待处理 ' + esc(count) + ' 个 STRM</p>'
    + '<p>' + (target === 'local' ? '联动删除本地 STRM 与 115 源文件。' : '仅删除分享库 STRM，保留云端源文件。') + '</p>'
    + '<p id="mappingDeleteStatus" role="status" aria-live="polite" style="color:var(--text-mid)"></p>'
    + '<progress id="mappingDeleteBar" value="0" max="' + Math.max(1, count) + '" style="width:100%"></progress>'
    + '<p id="mappingDeleteCounts" role="status" aria-live="polite">已处理 0 / ' + esc(count) + '</p>'
    + '<p id="mappingDeleteFile" style="word-break:break-all;color:var(--text-dim);font-size:12px"></p>'
    + '<button class="btn gray" onclick="closeModal()">关闭提示（删除继续执行）</button></div>');
  renderMappingDeleteState();
  hydratePosters($('modalBody'));
}
function updateMappingDeleteProgress(snapshot){
  var total = snapshot.total || (mappingDeleteView || {}).count || 0;
  var processed = Number(snapshot.processed) || 0;
  var phase = snapshot.phase;
  mappingDeletePhase(phase === 'cleanup' ? '正在清理空目录…' : phase === 'finalizing' ? '正在更新片库状态…' :
    (mappingDeleteView.target === 'local' ? '正在删除本地 STRM 及 115 源文件…' : '正在删除分享库 STRM…'));
  var bar = $('mappingDeleteBar');
  if (bar) {bar.max = Math.max(1, total); bar.value = processed;}
  var counts = $('mappingDeleteCounts');
  if (counts) counts.textContent = '已处理 ' + processed + ' / ' + total + ' · STRM 已删除 ' + (snapshot.strm_removed || 0)
    + (mappingDeleteView.target === 'local' ? ' · 115 源文件已删除 ' + (snapshot.cloud_removed || 0) : '')
    + ' · 保留 ' + (snapshot.retained || 0);
  var file = $('mappingDeleteFile');
  if (file) file.textContent = snapshot.current_file || '';
}
function mappingOperationId(){
  var bytes = new Uint8Array(16);
  if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(bytes);
  else for (var i=0;i<bytes.length;i++) bytes[i] = Math.floor(Math.random()*256);
  return Array.from(bytes).map(function(x){return ('0' + x.toString(16)).slice(-2);}).join('');
}
async function executeMappingDelete(path, body){
  var operationId = mappingOperationId();
  var statusPath = '/api/emby/delete/progress/' + operationId;
  var initial;
  try {
    initial = await api(path, {method:'POST', body:JSON.stringify(Object.assign({}, body, {async_delete:true, operation_id:operationId}))});
  } catch(error) {
    // Lost acceptance response: query this ID; never submit the deletion again.
    try {initial = await api(statusPath, {timeoutMs:10000, cache:'no-store'});} catch(_) {throw error;}
    if (initial.state === 'done') return initial.result;
    initial = {status:'accepted'};
  }
  if (initial.status !== 'accepted') return initial; // Compatible with synchronous servers.
  var failures = 0;
  while (true) {
    var snapshot;
    try {snapshot = await api(statusPath, {timeoutMs:10000, cache:'no-store'}); failures = 0;}
    catch(error) {
      failures++;
      mappingDeletePhase('进度连接暂时中断，正在查询结果；请勿重复提交…');
      if (failures >= 3) throw error;
      await new Promise(function(resolve){setTimeout(resolve, 2000);});
      continue;
    }
    updateMappingDeleteProgress(snapshot);
    if (snapshot.state === 'done') return snapshot.result;
    await new Promise(function(resolve){setTimeout(resolve, 1000);});
  }
}
function showMappingDeleteResult(title, message, details){
  openModal('<div class="mapping-detail"><h3>' + esc(title) + '</h3>'
    + mappingDeleteHeader(mappingDeleteEntry()) + '<p role="status" aria-live="polite">' + esc(message) + '</p>'
    + (details ? '<p style="white-space:pre-wrap;word-break:break-word;color:var(--text-mid)">' + esc(details) + '</p>' : '')
    + '<button class="btn gray" onclick="closeModal()">关闭</button></div>');
  hydratePosters($('modalBody'));
}
function finishMappingDelete(){
  mappingDeleteBusy = false;
  mappingDeleteMessage = '';
  renderMappingDeleteState();
}
function mappingDeleteOutcome(result, previewCount, target){
  // 使用执行时的数量。预览之后，另一设备可能已经删除了一部分或全部文件。
  var count = result.count == null ? previewCount : Number(result.count);
  count = Math.max(0, count || 0);
  var removed = Math.max(0, Number(result.strm_removed) || 0);
  var cloud = Math.max(0, Number(result.cloud_removed) || 0);
  var errors = Array.isArray(result.errors) ? result.errors.filter(function(x){ return String(x || '').trim(); }) : [];
  var noChange = count === 0 && removed === 0 && cloud === 0 && !errors.length;
  var remaining = Math.max(0, count - removed);
  var summary = '已删除 STRM ' + removed + ' 个' + (target === 'local' ? '，云端源文件 ' + cloud + ' 个' : '');
  return {count:count, removed:removed, cloud:cloud, errors:errors, remaining:remaining,
    noChange:noChange, partial:remaining > 0 || errors.length > 0, summary:summary};
}
function reportMappingDeleteResult(result, previewCount, target){
  var outcome = mappingDeleteOutcome(result, previewCount, target);
  if (outcome.noChange) {
    showMappingDeleteResult('文件已不存在', '本次未新增删除', '已按片库状态更新列表。');
    toast('该库文件已不存在，本次未新增删除', '', 4500);
    return;
  }
  var entry = mappingDeleteEntry();
  var other = entry && entry[target === 'local' ? 'in_share' : 'in_local'];
  var details = other ? (target === 'local' ? '分享库仍有文件，海报与分享库信息已保留。' : '本地库仍有文件，海报与本地库信息已保留。') : '';
  if (!outcome.partial && (outcome.removed > 0 || outcome.cloud > 0)) {
    showMappingDeleteResult('删除完成', outcome.summary, details);
    toast(outcome.summary, 'success', 4500);
    return;
  }
  details += '\n本次仍有 ' + outcome.remaining + ' 个 STRM 未删除\n'
    + (outcome.errors.length ? outcome.errors.slice(0, 3).join('\n') : '服务端未返回具体原因，请同步片库状态并查看日志。');
  showMappingDeleteResult('删除未全部完成', outcome.summary, details.trim());
  toast(outcome.summary + '，处理未全部完成', outcome.removed || outcome.cloud ? '' : 'error', 5000);
}
function reportMappingDeleteError(error, phase, result, target, count){
  var message;
  if (result && result.status === 'success') {
    var outcome = mappingDeleteOutcome(result, count, target);
    message = (outcome.noChange ? '本次未新增删除' : outcome.summary) + '\n删除结果已返回，但界面更新失败：' + error.message + '\n请读取片库缓存核实状态。';
  } else if (phase === 'delete') {
    message = '删除请求结果未确认：' + error.message + '\n部分文件可能已经删除。请等待并同步片库状态，不要重复提交。';
  } else if (phase === 'resync') {
    message = '同步片库状态失败（未新增删除）：' + error.message;
  } else {
    message = '核对文件失败（未提交删除）：' + error.message;
  }
  showMappingDeleteResult('删除状态', message, '');
}

function showMovieDetailById(id){
  var detailPending = mappingNeedDetail('movies', id);
  if (!embyLoaded) { toast('完整片库仍在读取，请稍后查看详情', '', 2500); return; }
  var m = (window.__embyMovieMap || {})[id];
  if (!m) return;
  var html = '';
  html += '<div style="display:flex;gap:16px;margin-bottom:16px">';
  html += '<div style="width:110px;height:165px;border-radius:8px;background:#f3f4f6;overflow:hidden;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:11px">';
  /* TMDB poster_path 优先（上游海报对照逻辑），Emby 缓存图兜底 */
  var mtp = m.poster_tmdb ? '/api/tmdb/poster' + m.poster_tmdb : '';
  html += (typeof posterDecodedMarkup === 'function' ? posterDecodedMarkup(mtp || '/api/emby/poster/' + m.id) : '') || (mtp
    ? '<img data-emby-src="' + esc(mtp) + '" style="width:100%;height:100%;object-fit:cover" onerror="this.onerror=null;this.style.display=\'none\';">'
    : (m.has_image ? '<img data-emby-src="/api/emby/poster/' + esc(m.id) + '" style="width:100%;height:100%;object-fit:cover">' : '\u65e0\u56fe'));
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

  html += '<section class="mapping-movie-libraries"><h4>两库版本</h4><div class="mapping-action-tools">' +
    '<div><b>本地库</b><p>' + (m.in_local ? '已入库' + (m.local_versions != null ? ' · '+esc(m.local_versions)+' 个版本' : ' · 版本数待补充') : '未入库') + '</p></div>' +
    '<div><b>分享库</b><p>' + (m.in_share ? '已入库' + (m.share_versions != null ? ' · '+esc(m.share_versions)+' 个版本' : ' · 版本数待补充') : '未入库') + '</p></div></div></section>';

  var embyHost = (window.__embyHost || '').replace(/\/+$/, '');
  html += '<div class="mapping-detail-actions"><div class="mapping-action-delete-row">';
  if (m.tmdb_id) {
    html += '<button class="btn mapping-delete" data-mapping-delete onclick="deleteMovieByTmdb(' + jsarg(m.tmdb_id) + ',\'local\',' + jsarg(m.name || '') + ')">删除本地库</button>';
    html += '<button class="btn mapping-delete" data-mapping-delete onclick="deleteMovieByTmdb(' + jsarg(m.tmdb_id) + ',\'share\',' + jsarg(m.name || '') + ')">删除分享库</button>';
  }
  html += '</div><div class="mapping-action-tools">';
  if (typeof tgtoButton === 'function') html += tgtoButton({title:m.name,type:'movie',tmdb_id:m.tmdb_id,year:m.year,poster:mtp || (m.has_image ? '/api/emby/poster/' + encodeURIComponent(m.id) : ''),library:(m.in_local ? '本地库' : '') + (m.in_share ? (m.in_local ? ' · 分享库' : '分享库') : '')});
  if (m.id && embyHost) {
    html += '<a href="' + esc(embyHost) + '/web/index.html#!/item?id=' + esc(m.id) + '" target="_blank" rel="noopener" style="text-decoration:none" class="btn gray">\u5728 Emby \u4e2d\u6253\u5f00</a>';
  }
  html += '</div><button class="btn gray mapping-action-close" onclick="closeModal()">关闭</button>';
  html += '</div>';
  html += '<div id="mappingDeleteStatus" role="status" aria-live="polite" hidden style="margin-top:12px;color:var(--text-mid)"></div>';

  openModal(html);
  mappingDetailPending('movies', id, detailPending);
  renderMappingDeleteState();
  hydratePosters($('modalBody'));
}

async function deleteMovieByTmdb(tmdbId, target, movieName){
  if (!beginMappingDelete()) return;
  var phase = 'preview', r2 = null, count = 0;
  var targetName = target === 'local' ? '\u672c\u5730\u5e93\uff08\u542b 115 \u6e90\u6587\u4ef6\uff09' : '\u5206\u4eab\u5e93';
  try {
    var r1 = await api('/api/emby/movie/delete_by_tmdb', {
      method: 'POST',
      body: JSON.stringify({ tmdb_id: tmdbId, target: target, dry_run: true, name: movieName })
    });
    if (r1.status !== 'success') throw new Error(r1.message || '\u5931\u8d25');
    count = r1.count || 0;
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

    phase = 'delete';
    showMappingDeleteProgress(target, movieName, count, 'movie', tmdbId);
    r2 = await executeMappingDelete('/api/emby/movie/delete_by_tmdb',
      {tmdb_id:tmdbId, target:target, dry_run:false, name:movieName});
    if (r2.status === 'busy') { showMappingDeleteResult('正在处理其他任务', r2.message || '已有治理任务正在执行，请稍后再试', '本次未提交删除。'); toast(r2.message || '已有治理任务正在执行，请稍后再试', '', 5000); return; }
    if (r2.status !== 'success') throw new Error(r2.message || '\u5931\u8d25');

    var outcome = mappingDeleteOutcome(r2, count, target);
    if (Array.isArray(r2.movie_entries)) {
      embyData.movies = embyData.movies.filter(function(x){return String(x.tmdb_id || '') !== String(tmdbId);})
        .concat(r2.movie_entries);
      window.__embyMovieMap = {};
      embyData.movies.forEach(function(x){if(x.id)window.__embyMovieMap[x.id] = x;});
      if (embyData.stats && !mappingPagedActive) embyData.stats.total_movies = embyData.movies.length;
      renderEmbyList();
    }
    // 有文件保留或结果为空时，不把海报当作全部删完而撤下。
    if (!Array.isArray(r2.movie_entries) && !outcome.partial && !outcome.noChange) {
      var flag = target === 'local' ? 'in_local' : 'in_share';
      embyData.movies = embyData.movies.filter(function(x){
        if (String(x.tmdb_id || '') !== String(tmdbId) || !x[flag]) return true;
        x[flag] = false;
        return !!x[target === 'local' ? 'in_share' : 'in_local'];
      });
      if (embyData.stats && !mappingPagedActive) embyData.stats.total_movies = embyData.movies.length;
      renderEmbyList();
    }
    reportMappingDeleteResult(r2, count, target);
  } catch(e){
    reportMappingDeleteError(e, phase, r2, target, count);
  } finally {
    finishMappingDelete();
  }
}

function showEmbyDetailById(id){
  var detailPending = mappingNeedDetail('series', id);
  if (!embyLoaded) { toast('完整片库仍在读取，请稍后查看详情', '', 2500); return; }
  var s = (window.__embySeriesMap || {})[id];
  if (!s) return;
  var ti = s.tmdb_info || {};
  var mst = ti.match_status || 'unmatched';
  var html = '<div class="mapping-detail">';

  html += '<div class="mapping-detail-header">';
  html += '<div class="mapping-detail-poster">';
  /* TMDB poster_path 优先（上游海报对照逻辑），Emby 缓存图兜底 */
  html += (typeof posterDecodedMarkup === 'function' ? posterDecodedMarkup(ti.poster ? '/api/tmdb/poster' + ti.poster : '/api/emby/poster/' + s.id) : '') || (ti.poster
    ? '<img data-emby-src="/api/tmdb/poster' + esc(ti.poster) + '" style="width:100%;height:100%;object-fit:cover" onerror="this.onerror=null;this.style.display=\'none\';">'
    : (s.has_image
      ? '<img data-emby-src="/api/emby/poster/' + esc(s.id) + '" style="width:100%;height:100%;object-fit:cover">'
      : '无图'));
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
  html += '<div class="mapping-detail-actions"><div class="mapping-action-tools">';
  if (typeof tgtoButton === 'function') html += tgtoButton({title:s.name,type:'tv',tmdb_id:s.tmdb_id || ti.tmdb_id,year:s.year,poster:ti.poster ? '/api/tmdb/poster' + ti.poster : (s.has_image ? '/api/emby/poster/' + encodeURIComponent(s.id) : ''),library:(s.in_local ? '本地 ' + (s.local_eps || 0) + ' 集' : '') + (s.in_share ? (s.in_local ? ' · ' : '') + '分享 ' + (s.share_eps || 0) + ' 集' : '')}, '找资源 / 补集');
  if (s._md) html += '<button class="btn gray" onclick="toggleManualDone(' + jsarg(s.id) + ')">取消完结</button>';
  else if (mst === 'missing' || mst === 'ongoing') html += '<button class="btn" onclick="toggleManualDone(' + jsarg(s.id) + ')">手动完结</button>';
  if (s.id && embyHost) {
    html += '<a href="' + esc(embyHost) + '/web/index.html#!/item?id=' + esc(s.id) + '" target="_blank" rel="noopener" style="text-decoration:none" class="btn gray">在 Emby 中打开</a>';
  }
  html += '</div><div class="mapping-action-delete-row">';
  html += '<button class="btn mapping-delete" data-mapping-delete onclick="deleteSeriesFromLib(' + jsarg(s.id) + ',\'local\',' + jsarg(s.name || '') + ')">删除本地库</button>';
  html += '<button class="btn mapping-delete" data-mapping-delete onclick="deleteSeriesFromLib(' + jsarg(s.id) + ',\'share\',' + jsarg(s.name || '') + ')">删除分享库</button>';
  html += '</div><button class="btn gray mapping-action-close" onclick="closeModal()">关闭</button>';
  html += '</div><div id="mappingDeleteStatus" role="status" aria-live="polite" hidden style="margin-top:12px;color:var(--text-mid)"></div></div>';

  openModal(html);
  mappingDetailPending('series', id, detailPending);
  renderMappingDeleteState();
  hydratePosters($('modalBody'));
  loadLibBreakdown(s, ti);
}

async function deleteSeriesFromLib(seriesId, target, seriesName){
  if (!beginMappingDelete()) return;
  var phase = 'preview', r2 = null, count = 0;
  var targetName = target === 'local' ? '\u672c\u5730\u5e93\uff08\u542b 115 \u6e90\u6587\u4ef6\uff09' : '\u5206\u4eab\u5e93';
  try {
    // 1. dry_run 先拿数量
    var r1 = await api('/api/emby/series/' + seriesId + '/delete', {
      method: 'POST',
      body: JSON.stringify({ target: target, dry_run: true, series_name: seriesName })
    });
    if (r1.status !== 'success') throw new Error(r1.message || '\u5931\u8d25');
    count = r1.count || 0;
    if (count === 0) {
      /* 该库没有文件：常见于 Emby / 缓存里残留了失效条目。提供「同步状态」，不删任何文件 */
      if (confirm('该库没有此剧文件。\n\n是否按磁盘实际情况同步状态？\n（不会删除任何文件；已无文件的剧会从列表移除）')) {
        phase = 'resync';
        mappingDeletePhase('正在同步片库状态，请稍候…');
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
    phase = 'delete';
    showMappingDeleteProgress(target, seriesName, count, 'series', seriesId);
    r2 = await executeMappingDelete('/api/emby/series/' + seriesId + '/delete',
      {target:target, dry_run:false, series_name:seriesName});
    if (r2.status === 'busy') { showMappingDeleteResult('正在处理其他任务', r2.message || '已有治理任务正在执行，请稍后再试', '本次未提交删除。'); toast(r2.message || '已有治理任务正在执行，请稍后再试', '', 5000); return; }
    if (r2.status !== 'success') throw new Error(r2.message || '\u5931\u8d25');

    var outcome = mappingDeleteOutcome(r2, count, target);
    if (outcome.noChange) {
      // 另一请求可能已完成；按磁盘事实同步，不发起新的删除。
      mappingDeletePhase('正在同步片库状态，请稍候…');
      var fresh = await api('/api/emby/series/' + seriesId + '/resync', {method:'POST', body:'{}'});
      if (fresh.status !== 'success') throw new Error(fresh.message || '同步失败');
      applySeriesDeleteResult(seriesId, fresh);
    } else {
      applySeriesDeleteResult(seriesId, r2);
    }
    reportMappingDeleteResult(r2, count, target);
  } catch(e){
    reportMappingDeleteError(e, phase, r2, target, count);
  } finally {
    finishMappingDelete();
  }
}

/* 只重新拉取单部剧集在 Emby 中的最新分集情况，替换/移除列表里对应的一条，
   避免像整页刷新那样把所有剧集的 TMDB 对照状态打回"待对照"。 */
function applySeriesDeleteResult(seriesId, r){
  var idx = embyData.series.findIndex(function(x){ return String(x.id) === String(seriesId) ||
    (x.series_ids || []).some(function(id){return String(id) === String(seriesId);}); });
  var oldId = idx >= 0 ? embyData.series[idx].id : seriesId;
  if (r.series_removed) {
    if (idx >= 0) embyData.series.splice(idx, 1);
    delete window.__embySeriesMap[seriesId];
    delete window.__embySeriesMap[oldId];
  } else if (r.entry && idx >= 0) {
    var md = embyData.series[idx]._md;
    if (md) r.entry._md = md;
    embyData.series[idx] = r.entry;
    delete window.__embySeriesMap[seriesId];
    delete window.__embySeriesMap[oldId];
    window.__embySeriesMap[r.entry.id] = r.entry;
  }
  embyData.stats.total_series = embyData.series.length;
  $('emby-total').textContent = embyData.stats.total_series || 0;
  __mappingSortedCache = {source:null, rows:null};
  renderEmbyList();
}

async function refreshSingleSeriesInList(seriesId, deletedTarget){
  if (mappingPagedActive) return loadMappingPaged(true);
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
    __mappingSortedCache = {source:null, rows:null};
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

  var mark = typeof mappingBatchMode !== 'undefined' && mappingBatchMode && mappingBatchAllowed() ? '<label class="mapping-batch-card-check" onclick="event.stopPropagation()"><input type="checkbox" aria-label="选择 '+esc(m.name||'电影')+'" '+(mappingBatchAll?'checked disabled ':(mappingBatchSelected.has(String(m.id))?'checked ':''))+'onchange="mappingBatchSetItem('+jsarg(m.id)+',this.checked)"></label>' : '';
  return '<div class="poster-card emby-poster-card" data-movie-id="' + esc(m.id) + '" onclick="showMovieDetailById(' + jsarg(m.id) + ')">'
    + mark + '<div class="poster-wrap">' + poster
    + '<span class="badge-type">电影</span>'
    + '</div>'
    + '<div class="info">'
    + '<div class="t" title="' + esc(m.name || '?') + '">' + esc(m.name || '?') + '</div>'
    + '<div class="meta"><span>' + (m.year || '—') + ' · ' + libTag + '</span></div>'
    + '</div>'
    + '</div>';
}


/* P1: SSE delivers tiny revision hints; /changes returns changed series and movies.
   Polling is a fallback for browsers/proxies without streaming support. */
var libraryLiveVersion = null;
var libraryLiveBusy = false;
function libraryLivePage(){
  var tab = window.__activeTab;
  return typeof document !== 'undefined' && !document.hidden &&
    (tab === 'mapping' || tab === 'explore' || tab === 'dashboard');
}
function patchVisibleMappingCard(item){
  var list = $('embyList');
  if (!list || typeof document === 'undefined') return;
  var nodes = list.querySelectorAll('.poster-card[data-series-id]');
  for (var i=0; i<nodes.length; i++) {
    var target = nodes[i];
    if (target.getAttribute('data-series-id') !== String(item.id)) continue;
    // Repaint text/badges only. Preserve the existing <img>, src, decoding and
    // in-flight image requests, so a new episode cannot cause poster flicker.
    var temp = document.createElement('div');
    temp.innerHTML = renderSeriesCard(item);
    var fresh = temp.firstElementChild;
    if (!fresh) return;
    var oldInfo = target.querySelector('.info'), newInfo = fresh.querySelector('.info');
    if (oldInfo && newInfo) oldInfo.innerHTML = newInfo.innerHTML;
    var oldBadge = target.querySelector('.badge-status'), newBadge = fresh.querySelector('.badge-status');
    if (oldBadge && newBadge) {
      oldBadge.className = newBadge.className;
      oldBadge.textContent = newBadge.textContent;
    }
    target.className = fresh.className;
  }
}
function patchVisibleMovieCard(item){
  var list = $('embyList');
  if (!list || typeof document === 'undefined') return;
  var nodes = list.querySelectorAll('.poster-card[data-movie-id]');
  for (var i=0; i<nodes.length; i++) {
    var target = nodes[i];
    if (target.getAttribute('data-movie-id') !== String(item.id)) continue;
    // Keep the existing poster <img> and Blob URL. Only replace its text.
    var temp = document.createElement('div');
    temp.innerHTML = renderMovieCard(item);
    var fresh = temp.firstElementChild;
    if (!fresh) return;
    var oldInfo = target.querySelector('.info'), newInfo = fresh.querySelector('.info');
    if (oldInfo && newInfo) oldInfo.innerHTML = newInfo.innerHTML;
    target.className = fresh.className;
  }
}
async function pollLibraryLiveChanges(){
  if (!libraryLivePage() || libraryLiveBusy) return;
  libraryLiveBusy = true;
  try {
    var r = await api('/api/library/changes?since=' + (libraryLiveVersion === null ? 0 : libraryLiveVersion),
                      {timeoutMs:8000});
    if (r.status !== 'success') return;
    if (mappingPagedActive) {
      if (libraryLiveVersion !== r.version) {
        libraryLiveVersion = r.version;
        mappingPagedDetails = Object.create(null);
        if (window.__activeTab === 'mapping') await loadMappingPaged(true);
        else mappingPagedDirty = true;
        if (window.__activeTab === 'explore' && typeof ensureExploreLibrarySync === 'function')
          ensureExploreLibrarySync(true);
        if (window.__activeTab === 'dashboard' && typeof loadDashboard === 'function')
          loadDashboard();
      }
      return;
    }
    // Even the first revision fetch may race with a newly committed episode.
    // Apply available deltas (or reconcile) instead of silently acknowledging
    // a revision the currently rendered page has never observed.
    var priorVersion = libraryLiveVersion;
    if (priorVersion !== null && priorVersion === r.version) return;
    if (priorVersion === null && r.version === 0) {
      libraryLiveVersion = 0;
      return;
    }
    // A full mapping request in flight might have started before this event.
    // Retry on next poll rather than letting its late response undo our patch.
    if (window.__activeTab === 'mapping' && embyLibraryLoading) return;
    libraryLiveVersion = r.version;
    if (r.full) {
      if (window.__activeTab === 'mapping' && embyLoaded && !embyLibraryLoading)
        await loadEmbyLibrary(false, true, true);
    } else if (((r.series && r.series.length) || (r.movies && r.movies.length)) && embyLoaded) {
      var added = false, oldStatus = {};
      (r.series || []).forEach(function(item){
        var idx = embyData.series.findIndex(function(x){return String(x.id) === String(item.id);});
        if (idx < 0) {
          idx = embyData.series.findIndex(function(x){
            return (x.series_ids || [x.id]).some(function(id){return (item.series_ids || [item.id]).indexOf(id) >= 0;});
          });
        }
        if (idx < 0) {embyData.series.push(item); added = true;}
        else {
          oldStatus[item.id] = (embyData.series[idx].tmdb_info || {}).match_status;
          embyData.series[idx] = item;
        }
      });
      var movieAdded = false;
      (r.movies || []).forEach(function(item){
        var ids = item.ids || [item.id];
        var idx = embyData.movies.findIndex(function(x){
          return ids.some(function(id){return (x.ids || [x.id]).some(function(old){return String(old) === String(id);});});
        });
        if (idx < 0) {embyData.movies.push(item); movieAdded = true;}
        else embyData.movies[idx] = item;
      });
      __mappingSortedCache = {source:null, rows:null};
      if (r.stats) embyData.statsBase = r.stats;
      embyData.stats = md_sync(embyData.series, embyData.statsBase || {});
      window.__embySeriesMap = {};
      embyData.series.forEach(function(x){if(x.id)window.__embySeriesMap[x.id]=x;});
      window.__embyMovieMap = {};
      embyData.movies.forEach(function(x){if(x.id)window.__embyMovieMap[x.id]=x;});
      if (window.__activeTab === 'mapping') {
        // Sorting/filter membership may change. Only those cases redraw a list.
        var movieTab = embyFilterState.type === 'movies';
        var reorder = (movieTab ? movieAdded : added) ||
                      (embyFilterState.filter !== 'all') || (embyFilterState.scope !== 'all');
        if (reorder) renderEmbyList();
        else if (movieTab) (r.movies || []).forEach(patchVisibleMovieCard);
        else (r.series || []).forEach(patchVisibleMappingCard);
        $('emby-title-count').textContent = movieTab ? embyData.movies.length : embyData.series.length;
        $('emby-total').textContent = movieTab ? embyData.movies.length : embyData.series.length;
        $('emby-title-movies').textContent = embyData.movies.length;
        $('emby-movies').textContent = embyData.movies.length;
        var st = embyData.stats;
        $('emby-aligned').textContent = st.aligned || 0;
        $('emby-missing').textContent = st.missing || 0;
        $('emby-extra').textContent = st.extra || 0;
        $('emby-ongoing').textContent = st.ongoing || 0;
        $('emby-unmatched').textContent = (st.unmatched || 0) + (st.no_tmdb || 0);
        updateEmbyChips();
      }
    }
    // Exploration already supports per-card status updates without remounting
    // posters; simply schedule it immediately after an authoritative change.
    if (window.__activeTab === 'explore' && typeof ensureExploreLibrarySync === 'function')
      ensureExploreLibrarySync(true);
    if (window.__activeTab === 'dashboard' && typeof loadDashboard === 'function')
      loadDashboard();
  } catch(e) { /* transient disconnects are handled by next event or poll */ }
  finally {libraryLiveBusy = false;}
}
(function(){
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  function start(){
    // Only one authenticated same-origin connection; no credentials in URLs.
    if (typeof EventSource !== 'undefined') {
      var feed = new EventSource('/api/library/events');
      feed.addEventListener('library', function(){pollLibraryLiveChanges();});
    }
    setInterval(pollLibraryLiveChanges, 30000);
    pollLibraryLiveChanges();
    document.addEventListener('visibilitychange', function(){
      if (!document.hidden) pollLibraryLiveChanges();
    });
  }
  if (document.readyState === 'loading') window.addEventListener('load', start);
  else start();
})();
