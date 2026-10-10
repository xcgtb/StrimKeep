/* ═══════════ 影视探索 ═══════════ */
var exploreState = { region: 'all', media: 'all', year: '', sort: 'popularity', genre: '', page: 1, totalPages: 1, q: '' };
var exploreLoading = false;
var exploreGeneration = 0;
var explorePages = {};
var exploreMoreFailed = false;
var exploreShownCards = [];
var exploreVisibleLimit = 20;
var explorePrefetch = null;
var exploreFiltersOpen = false;
var exploreSubscriptionsPromise = null;

var REGION_LABEL = { all: '全部地区', cn: '大陆', hk: '香港', tw: '台湾', jp: '日本', kr: '韩国', us: '欧美' };
var MEDIA_LABEL  = { all: '全部', movie: '电影', tv: '剧集' };
var SORT_LABEL   = { popularity: '热度', release: '最新', rating: 'TMDB 评分' };

function updateFilterSummaryText(){
  var parts = [
    REGION_LABEL[exploreState.region] || '全部地区',
    MEDIA_LABEL[exploreState.media]   || '电影',
    exploreState.genre || '全题材',
    exploreState.year ? (exploreState.year + ' 年') : '全部年份',
    SORT_LABEL[exploreState.sort]     || '热度'
  ];
  var el = $('filterSummaryText');
  if (el) el.textContent = parts.join(' · ');
}

function toggleExploreFilters(){
  exploreFiltersOpen = !exploreFiltersOpen;
  var panel = $('exploreFilters');
  var summary = $('filterSummary');
  var toggleText = $('filterToggleText');
  if (!panel) return;
  if (exploreFiltersOpen) {
    panel.classList.remove('hidden');
    if (summary) summary.classList.add('open');
    if (toggleText) toggleText.textContent = '收起筛选';
  } else {
    panel.classList.add('hidden');
    if (summary) summary.classList.remove('open');
    if (toggleText) toggleText.textContent = '展开筛选';
  }
}

/* 年份 chip：初始生成最近若干年，滚动到最右侧（更早年份）时继续往前补 */
var _yearMinLoaded = null;
var YEAR_FLOOR = 1900;
function _appendYearChips(fromYear, count){
  var el = $('filterYear');
  if (!el) return fromYear;
  var frag = document.createDocumentFragment();
  var y = fromYear;
  var n = 0;
  while (y >= YEAR_FLOOR && n < count) {
    var btn = document.createElement('button');
    btn.className = 'chip';
    btn.dataset.v = String(y);
    btn.textContent = y;
    frag.appendChild(btn);
    y -= 1; n += 1;
  }
  el.appendChild(frag);
  return y; // 下一次应从这个年份继续往前生成
}
function populateYearChips(){
  var el = $('filterYear');
  if (!el || el.dataset.populated) return;
  el.dataset.populated = '1';
  var thisYear = new Date().getFullYear() + 1;
  _yearMinLoaded = _appendYearChips(thisYear, 30);
  el.addEventListener('scroll', function(){
    if (_yearMinLoaded === null || _yearMinLoaded < YEAR_FLOOR) return;
    if (el.scrollLeft + el.clientWidth >= el.scrollWidth - 80) {
      _yearMinLoaded = _appendYearChips(_yearMinLoaded, 20);
    }
  }, { passive: true });
}

/* 题材 chip 会因所选「类型」(电影/剧集) 而有所不同，切换类型时隐藏不适用的题材 */
function refreshGenreChipsForMedia(media){
  var el = $('filterGenre');
  if (!el) return;
  var activeInvalid = false;
  el.querySelectorAll('.chip').forEach(function(b){
    var m = b.dataset.media || 'both';
    var visible = (m === 'both' || media === 'all' || m === media || !b.dataset.v);
    b.classList.toggle('hidden', !visible);
    if (!visible && b.classList.contains('active')) activeInvalid = true;
  });
  if (activeInvalid) {
    el.querySelectorAll('.chip').forEach(function(b){ b.classList.remove('active'); });
    var allBtn = el.querySelector('.chip[data-v=""]');
    if (allBtn) allBtn.classList.add('active');
    exploreState.genre = '';
  }
}

function setupChipGroup(id, key){
  var el = $(id);
  if (!el) return;
  el.addEventListener('click', function(e){
    var btn = e.target.closest('.chip');
    if (!btn) return;
    el.querySelectorAll('.chip').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    exploreState[key] = btn.dataset.v;
    if (key === 'media') refreshGenreChipsForMedia(exploreState.media);
    exploreState.page = 1;
    exploreState.q = '';
    $('exploreSearch').value = '';
    if (key === 'year') { var yi = $('filterYearInput'); if (yi) yi.value = ''; }
    updateFilterSummaryText();
    // 选完自动收起（移动端体验好；桌面端也一致）
    if (exploreFiltersOpen) toggleExploreFilters();
    loadExplore();
  });
}
function doExploreSearch(){
  var q = $('exploreSearch').value.trim();
  var retry = q === exploreState.q;
  exploreState.q = q; exploreState.page = 1;
  // 搜索时始终收起筛选栏
  if (exploreFiltersOpen) toggleExploreFilters();
  return loadExplore(false, retry);
}
function posterColumns(grid){
  if (!grid) return 1;
  var columns = getComputedStyle(grid).gridTemplateColumns.trim().split(/\s+/).filter(Boolean);
  return Math.max(1, Math.min(40, columns[0] === 'none' ? 1 : columns.length));
}
var exploreRequestController = null;
var exploreLoadedKey = '', exploreLoadedAt = 0, exploreSavedScroll = 0;
var exploreRefreshJobs = {};
var exploreLastResult = null;
function cancelExploreRefreshes(){
  Object.keys(exploreRefreshJobs).forEach(function(page){
    var job = exploreRefreshJobs[page];
    if (job.controller) job.controller.abort();
  });
  exploreRefreshJobs = {};
}
function pauseExplore(){
  exploreSavedScroll = window.scrollY || 0;
  exploreGeneration += 1;
  if (exploreRequestController) exploreRequestController.abort();
  if (explorePrefetch && explorePrefetch.controller) explorePrefetch.controller.abort();
  exploreRequestController = null;
  explorePrefetch = null;
  cancelExploreRefreshes();
  // 追加请求取消后，游标仍指向最后成功的页，返回时不会跳过一页。
  exploreState.page = Math.max(1, ...Object.keys(explorePages).map(Number));
  exploreLoading = false;
}
function ensureExploreLoaded(){
  // 返回同一筛选时保留卡片、海报和游标；不反复请求第一页或重建整个墙。
  if (exploreLoadedKey === exploreQuery(1) && Date.now() - exploreLoadedAt < 180000 && Object.keys(explorePages).length) {
    updateExploreLoadMore();
    ensureExplorePrefetch();
    requestAnimationFrame(function(){window.scrollTo(0, exploreSavedScroll);schedulePosterAutoLoad();});
    return;
  }
  exploreState.page = 1;
  return loadExplore();
}
function posterNextCount(shown, grid, batchSize){
  var columns = posterColumns(grid);
  return Math.max(shown + columns, Math.floor((shown + (batchSize || 40)) / columns) * columns);
}
function exploreAllCards(){
  var cards = [], seen = new Set();
  Object.keys(explorePages).sort(function(a,b){return Number(a)-Number(b);}).forEach(function(p){
    explorePages[p].forEach(function(card){
      var key = (card.type || '') + ':' + (card.tmdb_id || (card.title + ':' + card.year));
      if (!seen.has(key)) {seen.add(key);cards.push(card);}
    });
  });
  return cards;
}
function exploreHasMore(){
  return exploreAllCards().length > exploreShownCards.length || exploreState.page < exploreState.totalPages;
}
function updateExploreLoadMore(){
  var button = $('exploreMoreBtn');
  if (!button) return;
  var ready = Object.keys(explorePages).length > 0;
  // 自动加载为主；错误时提供明确重试，无 Observer 的浏览器保留按钮。
  button.hidden = !ready || !exploreHasMore() || (!exploreMoreFailed && !!window.IntersectionObserver);
  button.disabled = exploreLoading;
  button.textContent = exploreLoading ? '加载中...' : (exploreMoreFailed ? '重试加载下一批' : '加载更多');
}
function exploreQuery(page){
  return 'region=' + encodeURIComponent(exploreState.region)
    + '&year=' + encodeURIComponent(exploreState.year)
    + '&sort=' + encodeURIComponent(exploreState.sort)
    + '&media=' + encodeURIComponent(exploreState.media)
    + '&genre=' + encodeURIComponent(exploreState.genre)
    + '&page=' + page + '&q=' + encodeURIComponent(exploreState.q);
}
function ensureExplorePrefetch(){
  var page = exploreState.page + 1, generation = exploreGeneration;
  if (page > exploreState.totalPages || exploreMoreFailed) return;
  if (explorePrefetch && explorePrefetch.generation === generation && explorePrefetch.page === page) return;
  var url = '/api/explore?' + exploreQuery(page);
  var entry = {page:page, generation:generation, controller:typeof AbortController === 'function' ? new AbortController() : null};
  entry.promise = (async function(){
    var deadline = Date.now() + 25000;
    while (generation === exploreGeneration && Date.now() < deadline) {
      var r = await api(url, {timeoutMs: Math.min(8000, deadline - Date.now()), signal:entry.controller ? entry.controller.signal : undefined});
      if (generation !== exploreGeneration) return {status:'cancelled'};
      if (r.status === 'success') {
        warmPosterUrls((r.cards || []).map(function(c){return c.poster;}), function(){return generation === exploreGeneration;});
        return r;
      }
      if (r.status !== 'pending') return r;
      await new Promise(function(resolve){setTimeout(resolve, 1000);});
    }
    return {status:'error', message:'下一批加载较慢，请重试'};
  })().catch(function(error){return {status:'error', message:error.message};});
  explorePrefetch = entry;
}
function renderExploreBuffered(){
  var all = exploreAllCards(), columns = posterColumns($('exploreGrid'));
  var count = Math.min(exploreVisibleLimit, all.length);
  if (exploreState.page < exploreState.totalPages || count < all.length) {
    // 人物过滤/去重可能留下不足一行的结果，仍要显示而非空白。
    var fullRows = count - count % columns;
    if (fullRows) count = fullRows;
  }
  var cards = all.slice(0, count);
  syncExploreCards(cards, exploreShownCards);
  exploreShownCards = cards;
  return cards;
}
function exploreMore(){
  if (exploreLoading || !Object.keys(explorePages).length || !exploreHasMore()) return;
  exploreVisibleLimit = posterNextCount(exploreShownCards.length, $('exploreGrid'), 20);
  var all = exploreAllCards();
  if (all.length >= exploreVisibleLimit || exploreState.page >= exploreState.totalPages) {
    var cards = renderExploreBuffered();
    updateExplorePageInfo();
    updateExploreLoadMore();
    schedulePosterAutoLoad();
    return;
  }
  var retry = exploreMoreFailed;
  exploreState.page += 1;
  return loadExplore(true, retry);
}
function updateExplorePageInfo(result){
  if (result) exploreLastResult = result;
  result = result || exploreLastResult;
  var label = '已显示 ' + exploreShownCards.length + ' 部';
  if (exploreLoading) label += ' · 正在加载下一批…';
  else if (exploreMoreFailed) label += ' · 加载失败，点击重试';
  else label += exploreHasMore() ? ' · 下滑加载更多' : ' · 已全部加载';
  if (result && result.refresh_error) label += ' · 更新失败，显示缓存';
  else if (Object.keys(exploreRefreshJobs).length) label += ' · 片库状态后台更新中';
  $('explorePageInfo').textContent = label;
}
function refreshExplorePage(page, qs, generation){
  if (exploreRefreshJobs[page]) return;
  var job = {controller:typeof AbortController === 'function' ? new AbortController() : null};
  exploreRefreshJobs[page] = job;
  job.promise = (async function(){
    var deadline = Date.now() + 25000;
    while (generation === exploreGeneration && Date.now() < deadline) {
      await new Promise(function(resolve){setTimeout(resolve, 1000);});
      if (generation !== exploreGeneration || Date.now() >= deadline) return;
      var r = await api('/api/explore?' + qs, {timeoutMs:Math.min(8000, deadline - Date.now()), signal:job.controller ? job.controller.signal : undefined});
      if (generation !== exploreGeneration) return;
      if (r.status !== 'success') return;
      if (r.total_pages) exploreState.totalPages = r.total_pages;
      explorePages[page] = r.cards || [];
      renderExploreBuffered();
      updateExplorePageInfo(r);
      if (!r.refreshing || r.refresh_error) return;
    }
  })().catch(function(){ /* 保留已显示卡片；后台失败不打断浏览。 */ }).finally(function(){
    if (generation !== exploreGeneration) return;
    if (exploreRefreshJobs[page] === job) delete exploreRefreshJobs[page];
    updateExplorePageInfo();
    updateExploreLoadMore();
    schedulePosterAutoLoad();
  });
}
async function loadExplore(append, retry){
  if (append && exploreLoading) return;
  var generation = append ? exploreGeneration : ++exploreGeneration;
  var requestedPage = exploreState.page;
  var previousPages = explorePages;
  if (!append) {
    if (exploreRequestController) exploreRequestController.abort();
    if (explorePrefetch && explorePrefetch.controller) explorePrefetch.controller.abort();
    cancelExploreRefreshes();
    explorePages = {};
    exploreShownCards = [];
    explorePrefetch = null;
    exploreLastResult = null;
    exploreVisibleLimit = posterNextCount(0, $('exploreGrid'), 20);
  }
  var prefetched = append && explorePrefetch && explorePrefetch.generation === generation && explorePrefetch.page === requestedPage ? explorePrefetch : null;
  // 被消费的预取请求仍由前台持有，切页/离开时也能中止。
  var controller = prefetched ? prefetched.controller : (typeof AbortController === 'function' ? new AbortController() : null);
  exploreRequestController = controller;
  exploreLoading = true;
  exploreMoreFailed = false;
  updateExploreLoadMore();
  var grid = $('exploreGrid');
  if (!append) {
    grid.innerHTML = Array.from({length:exploreVisibleLimit}, function(){
      return '<div class="poster-card explore-skeleton" aria-hidden="true"><div class="poster-wrap"></div><div class="info"><div class="skeleton-title"></div><div class="skeleton-meta"></div></div></div>';
    }).join('');
    $('explorePageInfo').textContent = '正在加载探索结果…';
  } else {
    updateExplorePageInfo();
  }
  var qs = exploreQuery(requestedPage);
  var rendered = false;
  var deadline = Date.now() + (exploreState.q ? 15000 : 25000);
  function showPage(r){
    exploreLoadedKey = exploreQuery(1);
    exploreLoadedAt = Date.now();
    exploreState.totalPages = r.total_pages || 1;
    explorePages[requestedPage] = r.cards || [];
    var cards = renderExploreBuffered();
    rendered = true;
    updateExplorePageInfo(r);
    updateExploreLoadMore();
    ensureExplorePrefetch();
  }
  if (prefetched) explorePrefetch = null;
  try {
    while (generation === exploreGeneration) {
      var remaining = deadline - Date.now();
      if (remaining <= 0) break;
      var requestQuery = qs + (retry ? '&retry=1' : '');
      retry = false; // 仅用户主动重试的首个请求跳过失败冷却，轮询不重复发起刷新。
      var r = prefetched ? await prefetched.promise : await api('/api/explore?' + requestQuery, {timeoutMs: Math.min(8000, remaining), signal:controller ? controller.signal : undefined});
      prefetched = null;
      if (generation !== exploreGeneration) return;
      if (r.status === 'success') {
        showPage(r);
        if (r.refreshing && !r.refresh_error) refreshExplorePage(requestedPage, qs, generation);
        return; // 可见结果到达就释放加载锁，状态更新在独立任务里完成。
      } else if (r.status !== 'pending') {
        throw new Error(r.message || '加载失败');
      }
      if (!append && r.message) $('explorePageInfo').textContent = r.message;
      await new Promise(function(resolve){setTimeout(resolve, 1000);});
    }
    if (generation !== exploreGeneration) return;
    if (rendered) {
      $('explorePageInfo').textContent += ' · 可稍后刷新';
    } else {
      throw new Error('服务响应较慢，可稍后重试；后台仍在加载');
    }
  } catch(e){
    if (generation !== exploreGeneration) return;
    if (rendered) {
      $('explorePageInfo').textContent = '显示缓存 · ' + e.message;
    } else {
      if (append) {
        explorePages = previousPages;
        exploreState.page = Math.max(1, requestedPage - 1);
        exploreMoreFailed = true;
        toast(e.message, 'error', 4000);
      } else {
        $('explorePageInfo').textContent = '';
        grid.innerHTML = '<div class="list-empty">' + esc(e.message)
          + '<br><button class="btn btn-ghost" onclick="loadExplore(false,true)">重新加载</button></div>';
      }
    }
  } finally {
    if (generation === exploreGeneration) {
      if (exploreRequestController === controller) exploreRequestController = null;
      exploreLoading = false;
      updateExploreLoadMore();
      if (rendered || append) updateExplorePageInfo();
      schedulePosterAutoLoad();
    }
  }
}

function syncExploreCards(cards, previous){
  function structure(c){
    // 仅影响卡片结构的字段；缓存时间、版本及刷新标志变化无需重建海报。
    return [c.type, c.tmdb_id, c.title, c.year, c.poster, c.rating];
  }
  if (!previous || (!previous.length && cards.length) || previous.length > cards.length ||
      JSON.stringify(previous.map(structure)) !== JSON.stringify(cards.slice(0, previous.length).map(structure))) {
    renderExploreCards(cards);
    return;
  }
  if (cards.length > previous.length) appendExploreCards(cards.slice(previous.length));
  var changed = [];
  for (var i = 0; i < previous.length; i++) {
    if (JSON.stringify(exploreStatus(previous[i])) !== JSON.stringify(exploreStatus(cards[i]))) changed.push(i);
  }
  if (!changed.length) return;
  var badges = $('exploreGrid').querySelectorAll('.poster-card .badge-status');
  changed.forEach(function(index){
    var badge = badges[index];
    if (!badge) return;
    var status = exploreStatus(cards[index]);
    // 只替换文字标记，保留图片节点、已加载的海报和滚动位置。
    badge.outerHTML = '<span class="badge-status ' + status.cls + '"' + (status.tip || '') + '>' + status.status + '</span>';
  });
}

function appendExploreCards(cards){
  var grid = $('exploreGrid');
  var subSet = _subscribedTmdbIds();
  var html = cards.map(function(c){
    var st = exploreStatus(c);
    var status = st.status, cls = st.cls, statusTip = st.tip || '';
    var typeLabel = c.type === 'tv' ? '剧集' : '电影';
    var posterHtml;
    if (!c.poster) {
      posterHtml = '<div class="no-img">暂无海报</div>';
    } else if (c.poster.indexOf('/api/emby/poster/') === 0) {
      posterHtml = '<div class="poster-loading">海报加载中</div><img data-emby-src="' + esc(c.poster) + '" alt="" loading="lazy" decoding="async" onload="clearPosterLoading(this)">';
    } else {
      posterHtml = '<div class="poster-loading">海报加载中</div><img src="' + esc(c.poster) + '" alt="" loading="lazy" decoding="async" onload="clearPosterLoading(this)" onerror="posterImageError(this)">';
    }
    var isSub = subSet[String(c.tmdb_id)];
    var subBtn = c.type === 'tv'
      ? '<button class="sub-btn ' + (isSub ? 'on' : '') + '" data-tmdb="' + esc(c.tmdb_id) + '" data-title="' + esc(c.title) + '" data-poster="' + esc(c.poster || '') + '">' + (isSub ? '✓ 已订阅' : '+ 订阅') + '</button>'
      : '';
    return '<div class="poster-card">'
      + '<div class="poster-wrap">' + posterHtml
      + '<span class="badge-type">' + typeLabel + '</span>'
      + (c.rating ? '<span class="badge-rate">' + icon('star') + c.rating + '</span>' : '')
      + '<span class="badge-status ' + cls + '"' + statusTip + '>' + status + '</span>'
      + '</div>'
      + '<div class="info">'
      + '<div class="t" title="' + esc(c.title) + '">' + esc(c.title) + '</div>'
      + '<div class="meta"><span>' + (c.year || '—') + ' · TMDB</span>' + subBtn + '</div>'
      + '</div>'
      + '</div>';
  }).join('');
  grid.insertAdjacentHTML('beforeend', html);
  hydratePosters(grid);
}

function warmPosterUrls(urls, active){
  // 只预热下一屏；不让离屏整批图片挤占当前可见海报的带宽。
  if (typeof navigator !== 'undefined' && navigator.connection && navigator.connection.saveData) return;
  urls = Array.from(new Set(urls.filter(Boolean))).slice(0, 12);
  var next = 0;
  async function worker(){
    while (next < urls.length && active()) {
      var url = urls[next++];
      try {
        if (url.indexOf('/api/emby/poster/') === 0) {
          await posterFetch(url);
        } else {
          await new Promise(function(resolve){
            var img = new Image(), timer = setTimeout(function(){done();img.src='';},8000);
            function done(){clearTimeout(timer);img.onload=img.onerror=null;resolve();}
            img.onload=img.onerror=done;img.src=url;
          });
        }
      } catch(e) {}
    }
  }
  for (var i=0; i<Math.min(2,urls.length); i++) worker();
}
function prefetchMappingPosters(){
  var view=embyView, from=embyShown;
  if (!view.fn || from >= view.arr.length) return;
  if (mappingPrefetch && mappingPrefetch.view === view && mappingPrefetch.from === from) return;
  mappingPrefetch={view:view,from:from};
  var urls=view.arr.slice(from, Math.min(view.arr.length,posterNextCount(from,$('embyList')))).map(function(item){
    var poster=item.poster_tmdb || ((item.tmdb_info || {}).poster);
    return poster ? 'https://image.tmdb.org/t/p/w500' + poster : (item.has_image ? '/api/emby/poster/' + item.id : '');
  });
  warmPosterUrls(urls,function(){return embyView === view && embyShown === from;});
}
var posterAutoFrame = null;
function schedulePosterAutoLoad(){
  if (posterAutoFrame !== null) return;
  posterAutoFrame=requestAnimationFrame(function(){posterAutoFrame=null;checkPosterAutoLoad();});
}
function checkPosterAutoLoad(){
  [['tab-explore','explorePager',function(){if(!exploreMoreFailed)exploreMore();}],
   ['tab-mapping','embyLoadMoreWrap',function(){if(!embyMoreFailed)embyMore();}]].forEach(function(entry){
    var tab=$(entry[0]), footer=$(entry[1]);
    if (!tab || !tab.classList.contains('active') || !footer || footer.style.display === 'none') return;
    var rect=footer.getBoundingClientRect();
    if (rect.top <= window.innerHeight + 900 && rect.bottom >= 0) entry[2]();
  });
}
function setupPosterAutoLoad(){
  if (window.__posterAutoReady) return;
  window.__posterAutoReady=true;
  if (window.IntersectionObserver) {
    var observer=new IntersectionObserver(schedulePosterAutoLoad,{rootMargin:'900px 0px',threshold:0});
    ['explorePager','embyLoadMoreWrap'].forEach(function(id){var footer=$(id);if(footer)observer.observe(footer);});
  }
  window.addEventListener('scroll',schedulePosterAutoLoad,{passive:true});
  window.addEventListener('resize',schedulePosterAutoLoad,{passive:true});
}

function _subscribedTmdbIds(){
  var set = {};
  currentSubs.forEach(function(s){ if (s.tmdb_id) set[String(s.tmdb_id)] = true; });
  return set;
}
function refreshExploreSubscriptionButtons(){
  var grid = $('exploreGrid');
  if (!grid) return;
  var subscribed = _subscribedTmdbIds();
  grid.querySelectorAll('.sub-btn').forEach(function(button){
    var on = !!subscribed[button.dataset.tmdb];
    button.classList.toggle('on', on);
    button.textContent = on ? '✓ 已订阅' : '+ 订阅';
  });
}
function libraryEpisodeTotal(info){
  return info.declared_total != null ? info.declared_total : info.tmdb_total;
}
function libraryEpisodeCount(series){
  return series.have_eps != null ? series.have_eps : (series.total_episodes || 0);
}
function libraryFactsLabel(s){
  var ts = s.facts_ts;
  var text = ts ? '集数更新 ' + new Date(ts * 1000).toLocaleString('zh-CN') : '集数尚未同步';
  if (s.stale || s.facts_stale) text += ' · TMDB 对照已过期';
  return text;
}
function exploreStatus(c){
  if (c.type === 'tv') {
    if (!c.eps) {
      if (c.eps_source === 'ambiguous') return {status:'身份冲突', cls:'no'};
      if (!(c.in_emby || c.in_local || c.in_share) && (c.library_status === 'unavailable' || c.library_status === 'stale')) return {status:'片库待同步', cls:'no'};
      return c.in_emby || c.in_local || c.in_share ? {status:'集数待同步', cls:'no'} : {status:'未入库', cls:'no'};
    }
    var stale = c.facts_stale ? '*' : '';
    var tip = ' title="' + esc('片库缓存 · ' + libraryFactsLabel(c)) + '"';
    var have = c.eps.have;
    var count = c.eps.total > 0 ? have + '/' + c.eps.total + ' 集' : have + ' 集';
    if (c.eps.manual_done) return {status:'完整 · ' + count + stale, cls:'ok', tip:tip};
    if (have <= 0) return {status:'未入库' + stale, cls:'no', tip:tip};
    if (c.eps.match_status === 'aligned') return {status:'完整 · ' + count + stale, cls:'ok', tip:tip};
    if (c.eps.match_status === 'extra') return {status:'超出对照 · ' + count + stale, cls:'share', tip:tip};
    return {status:'已入库 ' + count + stale, cls:'partial', tip:tip};
  }
  if (c.in_local || c.in_share) return { status: '已完整', cls: 'ok' };
  if ((c.library_status === 'unavailable' || c.library_status === 'stale')) return { status: '片库待同步', cls: 'no' };
  return { status: '未入库', cls: 'no' };
}

function renderExploreCards(cards){
  var grid = $('exploreGrid');
  if (!cards.length) { grid.innerHTML = '<div class="list-empty">无结果</div>'; return; }
  var subSet = _subscribedTmdbIds();
  grid.innerHTML = cards.map(function(c){
    var st = exploreStatus(c);
    var status = st.status, cls = st.cls, statusTip = st.tip || '';
    var typeLabel = c.type === 'tv' ? '剧集' : '电影';
    /* poster 有两种来源：
       1) TMDB 图片 → 直接 <img src>
       2) /api/emby/poster/... → 需要 Basic Auth，走 data-emby-src，由 hydratePosters() 拉 blob */
    var posterHtml;
    if (!c.poster) {
      posterHtml = '<div class="no-img">暂无海报</div>';
    } else if (c.poster.indexOf('/api/emby/poster/') === 0) {
      posterHtml = '<div class="poster-loading">海报加载中</div><img data-emby-src="' + esc(c.poster) + '" alt="" loading="lazy" decoding="async" onload="clearPosterLoading(this)">';
    } else {
      posterHtml = '<div class="poster-loading">海报加载中</div><img src="' + esc(c.poster) + '" alt="" loading="lazy" decoding="async" onload="clearPosterLoading(this)" onerror="posterImageError(this)">';
    }
    var isSub = subSet[String(c.tmdb_id)];
    var subBtn = c.type === 'tv'
      ? '<button class="sub-btn ' + (isSub ? 'on' : '') + '" data-tmdb="' + esc(c.tmdb_id) + '" data-title="' + esc(c.title) + '" data-poster="' + esc(c.poster || '') + '">' + (isSub ? '✓ 已订阅' : '+ 订阅') + '</button>'
      : '';
    return '<div class="poster-card">'
      + '<div class="poster-wrap">' + posterHtml
      + '<span class="badge-type">' + typeLabel + '</span>'
      + (c.rating ? '<span class="badge-rate">' + icon('star') + c.rating + '</span>' : '')
      + '<span class="badge-status ' + cls + '"' + statusTip + '>' + status + '</span>'
      + '</div>'
      + '<div class="info">'
      + '<div class="t" title="' + esc(c.title) + '">' + esc(c.title) + '</div>'
      + '<div class="meta"><span>' + (c.year || '—') + ' · TMDB</span>' + subBtn + '</div>'
      + '</div>'
      + '</div>';
  }).join('');
  hydratePosters(grid);
}
async function toggleSubscribe(tmdbId, title, poster){
  // 首屏不等订阅列表，但写入整份订阅前必须取得列表，防止覆盖已有订阅。
  try {
    if (exploreSubscriptionsPromise && !await exploreSubscriptionsPromise) {
      var initial = await api('/api/subscriptions', {timeoutMs:8000});
      if (initial.status !== 'success') throw new Error(initial.message || '订阅列表加载失败');
      currentSubs = initial.subscriptions || [];
      exploreSubscriptionsPromise = Promise.resolve(true);
    }
  } catch(e){ toast('加载订阅失败: ' + e.message, 'error'); return; }
  var idx = currentSubs.findIndex(function(s){ return String(s.tmdb_id) === String(tmdbId); });
  if (idx >= 0) {
    if (!(confirm('取消订阅《' + title + '》？'))) return;
    currentSubs.splice(idx, 1);
  } else {
    currentSubs.push({ id: tmdbId, tmdb_id: tmdbId, name: title, poster: poster || '', enabled: true });
    toast('已订阅《' + title + '》', 'success');
  }
  try {
    await api('/api/subscriptions', { method: 'POST', body: JSON.stringify({ subscriptions: currentSubs }) });
    refreshExploreSubscriptionButtons();
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
function switchExploreTab_init(){
  populateYearChips();
  refreshGenreChipsForMedia(exploreState.media);
  setupChipGroup('filterRegion', 'region');
  setupChipGroup('filterMedia', 'media');
  setupChipGroup('filterYear', 'year');
  setupChipGroup('filterSort', 'sort');
  setupChipGroup('filterGenre', 'genre');
  var yi = $('filterYearInput');
  if (yi) {
    yi.addEventListener('keydown', function(e){
      if (e.key !== 'Enter') return;
      var v = yi.value.trim();
      if (!v) return;
      exploreState.year = v; exploreState.page = 1; exploreState.q = '';
      $('exploreSearch').value = '';
      document.querySelectorAll('#filterYear .chip').forEach(function(b){ b.classList.remove('active'); });
      updateFilterSummaryText();
      if (exploreFiltersOpen) toggleExploreFilters();
      loadExplore();
    });
  }
  $('exploreSearch').addEventListener('keydown', function(e){ if (e.key === 'Enter') doExploreSearch(); });
  updateFilterSummaryText();
}

