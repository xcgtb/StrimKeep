/* ═══════════ 初始化 ═══════════ */

// 接管订阅按钮点击事件，避免内联 onclick 的注入风险
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.sub-btn');
  if (!btn) return;
  e.stopPropagation();
  var tmdbId = btn.getAttribute('data-tmdb');
  var title = btn.getAttribute('data-title');
  var poster = btn.getAttribute('data-poster') || '';
  toggleSubscribe(tmdbId, title, poster);
});

// 接管豁免关键词标签的删除点击，同样避免内联 onclick
document.addEventListener('click', function(e) {
  var rm = e.target.closest('.rm[data-kw]');
  if (!rm) return;
  removeExempt(rm.getAttribute('data-kw'));
});

document.addEventListener('DOMContentLoaded', async function(){
  initTheme();
  injectIcons();
  window.__activeTab = 'dashboard';
  renderMenu();
  switchExploreTab_init();
  buildSubnavs();
  // 先用本地缓存的数据瞬间填满治理总览，避免每次打开/刷新都先看到一堆
  // "-"/"检查中..."占位符再变成真实数据，视觉上不再像"每次都在重新刷新"。
  hydrateDashboardFromCache();
  // 订阅列表独立加载，慢请求不阻塞首屏或恢复到影视探索。
  exploreSubscriptionsPromise = api('/api/subscriptions', {timeoutMs:8000}).then(function(r){
    if (r.status !== 'success') return false;
    currentSubs = r.subscriptions || [];
    refreshExploreSubscriptionButtons();
    return true;
  }).catch(function(){return false;});
  var lastTab = 'dashboard';
  try { lastTab = sessionStorage.getItem('lastTab') || 'dashboard'; } catch(e){}
  if (lastTab !== 'dashboard' && $('tab-' + lastTab)) {
    switchTab(lastTab);
  } else {
    loadDashboard();
  }
  setupPosterAutoLoad();
  loadLibraryStats();
  pollTmdbProgress();
  startRuntimeStatus();
});
