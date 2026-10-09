/* ═══════════ 追更订阅 ═══════════ */
function renderSubscriptionCheckStatus(st){
  st = st || {};
  var state = st.status || 'idle';
  var labels = {running:'检查中',success:'正常',partial:'部分失败',error:'失败',disabled:'已停用',idle:'尚未运行'};
  var stateEl=$('subCheckState'), timeEl=$('subCheckTime'), resultEl=$('subCheckResult');
  if(stateEl) stateEl.textContent='检查状态：' + (labels[state] || state);
  if(timeEl){
    var ts=st.finished_at || st.ts || st.started_at;
    timeEl.textContent=ts ? ('时间：' + new Date(Number(ts)*1000).toLocaleString()) : '时间：—';
  }
  if(resultEl){
    if(state==='error' || state==='partial') resultEl.textContent='原因：' + (st.error || '请查看逐部原因') + (st.failed ? ' · ' + st.failed + ' 部失败/不完整' : '');
    else {
      var _chk = (st.checked != null) ? st.checked : (st.total || 0);
      resultEl.textContent='结果：检查 ' + _chk + ' 部 · ' + (st.updates || 0) + ' 部有变化' +
        (st.paused ? '（另有 ' + st.paused + ' 部已暂停，未检查）' : '');
    }
  }
}
async function loadSubscriptionCheckStatus(){
  try { var r=await api('/api/subscriptions/status'); if(r.status==='success') renderSubscriptionCheckStatus(r.check); } catch(e) {}
}

async function loadSubscriptions(){
  try {
    var r = await api('/api/subscriptions');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    currentSubs = r.subscriptions || [];
    $('subEnabled').checked = !!r.enabled;
    $('subCheckTmdb').checked = !!r.check_tmdb;
    $('subInterval').value = r.interval_min || 30;
    $('subCount').textContent = currentSubs.length + ' 部';

    if (!currentSubs.length) {
      $('subList').innerHTML = '<div class="list-empty">暂无订阅。去「影视探索」搜索剧集，点「+ 订阅」</div>';
    } else {
      $('subList').innerHTML = currentSubs.map(function(s, i){
        /* 订阅卡的海报也可能是 Emby poster URL，走 data-emby-src；TMDB 图片直接 src */
        var poster;
        if (!s.poster) {
          poster = '';
        } else if (s.poster.indexOf('/api/emby/poster/') === 0) {
          poster = '<img data-emby-src="' + esc(s.poster) + '" loading="lazy">';
        } else {
          poster = '<img src="' + esc(s.poster) + '" loading="lazy" onerror="this.style.display=\'none\'">';
        }
        var subTot = s.tmdb_declared != null ? s.tmdb_declared : s.tmdb_total;
        var tmdbExtra = subTot != null ? ' · TMDB ' + subTot + ' 集' : ' · TMDB 待同步';
        var st = s.tmdb_status ? ' · ' + s.tmdb_status : '';
        return '<div class="sub-item">'
          + '<div class="p">' + poster + '</div>'
          + '<div class="body">'
          + '<div class="name">' + esc(s.name) + '</div>'
          + '<div class="meta">当前入库: ' + (s.have_eps != null ? s.have_eps + ' 集' : (s.facts_status === 'ambiguous' ? '身份冲突' : '待同步')) + tmdbExtra + esc(st) + '</div>'
          + '<div class="meta">已通知至: <code>' + esc(s.latest_ep || '尚无记录') + '</code></div>'
          + '<div class="meta">' + esc(libraryFactsLabel(s)) + '</div>'
          + (s.check_error ? '<div class="meta" style="color:#f59e0b">检查失败/不完整：' + esc(s.check_error) + '</div>' : '')
          + (s.enabled ? '' : '<div class="meta" style="color:#f59e0b">⏸ 已暂停：不会检查新集，也不会推送</div>')
          + '</div>'
          + '<div class="actions">'
          + '<label class="switch" title="' + (s.enabled ? '追更中，点击暂停' : '已暂停，点击恢复') + '"><input type="checkbox"' + (s.enabled ? ' checked' : '') + ' onchange="toggleSubEnabled(' + i + ')"><span class="slider"></span></label>'
          + '<button class="btn sm red" onclick="removeSub(' + i + ')">删</button>'
          + '</div>'
          + '</div>';
      }).join('');
      hydratePosters($('subList'));
    }
    loadSubscriptionCheckStatus();
  } catch(e){ toast('加载订阅失败: ' + e.message, 'error', 4000); }
}
async function saveSubSettings(){
  try {
    await api('/api/subscriptions/settings', { method: 'POST', body: JSON.stringify({
      enabled: $('subEnabled').checked,
      check_tmdb: $('subCheckTmdb').checked,
      interval_min: parseInt($('subInterval').value) || 30,
    })});
    toast('设置已保存', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
async function toggleSubEnabled(i){
  currentSubs[i].enabled = !currentSubs[i].enabled;
  try {
    await api('/api/subscriptions', { method: 'POST', body: JSON.stringify({ subscriptions: currentSubs }) });
    loadSubscriptions();
  } catch(e){ toast(e.message, 'error'); }
}
async function removeSub(i){
  if (!(confirm('删除订阅《' + currentSubs[i].name + '》？'))) return;
  currentSubs.splice(i, 1);
  try {
    await api('/api/subscriptions', { method: 'POST', body: JSON.stringify({ subscriptions: currentSubs }) });
    loadSubscriptions();
  } catch(e){ toast(e.message, 'error'); }
}
async function checkSubsNow(){
  toast('正在检查...');
  try {
    var r = await api('/api/subscriptions/check', { method: 'POST' });
    if (r.status !== 'success' && r.status !== 'partial' && r.status !== 'disabled' && !r.rows) throw new Error(r.message || r.error || '失败');
    var ups = r.updates || [], failed = (r.rows || []).filter(function(x){return x.status === 'error' || x.status === 'partial';});
    var issues = failed.map(function(x){return '<div class="list-item"><div class="t">⚠️ 《' + esc(x.name || '') + '》</div><div class="d">' + esc(x.error || '读取不完整') + '</div></div>';}).join('');
    $('subUpdatesPanel').style.display = ups.length || failed.length ? '' : 'none';
    $('subUpdates').innerHTML = ups.map(function(u){
      var parts = [];
      if (u.refilled && u.refilled.length) parts.push('✅ 已补齐 ' + esc(u.refilled.join(', ')));
      if (u.new_eps && u.new_eps.length) parts.push('🆕 新增 ' + esc(u.new_eps.join(', ')));
      if (u.newly_missing && u.newly_missing.length) parts.push('⚠️ 新缺 ' + esc(u.newly_missing.join(', ')));
      return '<div class="list-item exempt"><div class="t">📺 《' + esc(u.name) + '》</div><div class="d">' + parts.join(' · ') + '</div></div>';
    }).join('') + issues;
    if (r.status === 'error' || r.status === 'partial') toast((r.error || '检查未全部成功') + ' · ' + ups.length + ' 部已确认变化', 'error', 5000);
    else if (r.skipped === 'disabled') toast('订阅检查已停用');
    else if (!r.checked && r.paused) toast('所有订阅已暂停，本次未检查');
    else toast(ups.length ? '发现 ' + ups.length + ' 部变化' : '检查完成：暂无新变化', 'success');
    await loadSubscriptions();
    renderSubscriptionCheckStatus(Object.assign({}, r, {finished_at:Date.now()/1000, updates:ups.length}));
  } catch(e){ toast('检查失败: ' + e.message, 'error'); renderSubscriptionCheckStatus({status:'error', ts:Date.now()/1000, error:e.message}); }
}

/* ═══════════ 每日晨报 ═══════════ */
async function loadMorning(){
  try {
    var r = await api('/api/morning');
    var mr = r.morning || {};
    $('mrEnabled').checked = !!mr.enabled;
    $('mrHour').value = mr.hour != null ? mr.hour : 9;
    $('mrMinute').value = mr.minute != null ? mr.minute : 0;
    $('mrPrescan').value = mr.prescan_min != null ? mr.prescan_min : 5;
    var tz = r.tz;
    if (tz && $('mrTzHint')) $('mrTzHint').textContent = '24 小时制 · 服务器时区 ' + tz.offset + '（' + tz.name + '），现在 ' + String(tz.now).slice(11, 16);
    var items = mr.items || [];
    $('mrItemStats').checked = items.indexOf('stats') >= 0;
    $('mrItemSubs').checked = items.indexOf('subscriptions') >= 0;
    $('mrItemGap').checked = items.indexOf('emby_gap') >= 0;
  } catch(e){ toast('加载晨报设置失败: ' + e.message, 'error'); }
}
async function saveMorning(){
  var items = [];
  if ($('mrItemStats').checked) items.push('stats');
  if ($('mrItemSubs').checked)  items.push('subscriptions');
  if ($('mrItemGap').checked)   items.push('emby_gap');
  try {
    await api('/api/morning', { method: 'POST', body: JSON.stringify({
      enabled: $('mrEnabled').checked,
      hour: parseInt($('mrHour').value) || 0,
      minute: parseInt($('mrMinute').value) || 0,
      prescan_min: parseInt($('mrPrescan').value) || 0,
      items: items,
    })});
    toast('已保存', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
async function previewMorning(force){
  var items = [];
  if ($('mrItemStats').checked) items.push('stats');
  if ($('mrItemSubs').checked)  items.push('subscriptions');
  if ($('mrItemGap').checked)   items.push('emby_gap');
  try {
    if (force) toast('现场扫描中，可能稍慢...');
    else toast('读取现有缓存...');
    var r = await api('/api/morning/preview', { method: 'POST', body: JSON.stringify({ items: items, force: force }) });
    if (r.status !== 'success') throw new Error(r.message || '失败');
    $('mrPreviewPanel').style.display = '';
    $('mrPreview').textContent = r.text || '(空)';
  } catch(e){ toast(e.message, 'error'); }
}
async function sendMorningNow(){
  if (!(confirm('立即发送晨报到 Telegram？（读取当前缓存，不现场扫描）'))) return;
  toast('生成并发送中...');
  try {
    var r = await api('/api/morning/send', { method: 'POST', body: JSON.stringify({ force: false }) });
    toast(r.message || '完成', r.status === 'success' ? 'success' : 'error');
  } catch(e){ toast(e.message, 'error'); }
}

