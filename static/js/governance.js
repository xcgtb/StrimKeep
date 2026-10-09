/* ═══════════ 扫描 ═══════════ */
async function refreshCaches(){
  try {
    var r = await api('/api/cache/refresh', { method: 'POST' });
    if (r.status !== 'success') throw new Error(r.message || '失败');
    toast('缓存已清除，后台重建中', 'success');
    loadDashboard();
  } catch(e){
    toast('刷新失败: ' + e.message, 'error');
  }
}

async function scanLibrary(){
  if (__scanBusy) return;
  __scanBusy = true;
  var control = {taskId:null, requested:false, accepted:false, sending:false, wake:null};
  __taskHintControl = control;
  showTaskHint('扫描双库中…');
  var btn1 = $('scanBtn'), btn2 = $('govScanBtn');
  [btn1, btn2].forEach(function(b){ if(b) b.disabled = true; });
  if (btn1) btn1.innerHTML = '<span class="spin"></span><span>扫描中...</span>';
  if (btn2) btn2.innerHTML = '<span class="spin"></span><span>扫描中...</span>';
  try {
    var res = await api('/api/check', { method: 'POST' });
    control.taskId = res.task_id;
    if (control.requested) requestScanCancel(control);
    var t = await pollTask(res.task_id, '扫描双库中...', control);
    if (t.status === 'cancelled') { toast('本次双库扫描已取消'); return; }
    if (t.status !== 'success') throw new Error(t.error || '诊断失败');
    var r = t.result;
    currentPlan = r.plan_id;
    govScanTs = Date.now() / 1000;
    govPlanExpireAt = currentPlan ? Date.now() + govPlanTtlMs : 0;
    if (currentPlan) tickGovBanner(); else setGovBanner('刚刚扫描 · 无待清理项');
    if ($('govExecBtn')) $('govExecBtn').disabled = !currentPlan;
    govData = {
      loc: r.del_local_items || [],
      shr: r.del_share_items || [],
      keep: r.protected_items || [],
      exempt: r.exempted_items || [],
      exemptTotal: r.exempted_count
    };
    $('govStats').style.display = '';
    $('gov-keep').textContent  = govData.keep.length;
    $('gov-loc').textContent   = govData.loc.length;
    $('gov-shr').textContent   = govData.shr.length;
    $('gov-exempt').textContent = (govData.exemptTotal != null ? govData.exemptTotal : govData.exempt.length);
    renderGovList();
    saveGovStateToSession();
    loadGovTruth();
    toast('扫描完成：待处理 ' + (r.total_clean_cnt || 0) + ' 项', 'success');
  } catch(e){ toast(e.message, 'error', 4000); }
  finally {
    __scanBusy = false;
    if (__taskHintControl === control) __taskHintControl = null;
    if (__hintPolls === 0) hideTaskHint();
    [btn1, btn2].forEach(function(b){ if(b) b.disabled = false; });
    if (btn1){ btn1.removeAttribute('data-icon-done'); btn1.innerHTML = '立即扫描双库'; btn1.setAttribute('data-icon','refresh'); }
    if (btn2){ btn2.removeAttribute('data-icon-done'); btn2.innerHTML = '重新扫描';   btn2.setAttribute('data-icon','refresh'); }
    injectIcons();
  }
}
function setGovFilter(f){
  currentGovFilter = f;
  document.querySelectorAll('#govFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === f); });
  renderGovList();
}
function renderGovList(){
  var items = [];
  if (currentGovFilter === 'all' || currentGovFilter === 'loc')
    govData.loc.forEach(function(t){ items.push({type:'loc', data:t}); });
  if (currentGovFilter === 'all' || currentGovFilter === 'shr')
    govData.shr.forEach(function(t){ items.push({type:'shr', data:t}); });
  if (currentGovFilter === 'all' || currentGovFilter === 'keep')
    govData.keep.forEach(function(t){ items.push({type:'keep', data:t}); });
  if (currentGovFilter === 'all' || currentGovFilter === 'exempt')
    govData.exempt.forEach(function(t){ items.push({type:'exempt', data:t}); });
  currentGovItems = items;
  var list = $('planList');
  if (!items.length) {
    list.innerHTML = '<div class="list-empty">' + ((govData.loc.length + govData.shr.length + govData.keep.length + govData.exempt.length) ? '当前筛选下无匹配项' : '无待治理项') + '</div>';
    return;
  }
  list.innerHTML = items.map(function(it, i){
    return '<div class="list-item '+it.type+' clickable" onclick="showGovDetailAt('+i+')">' +
           '<div class="t">' + esc(it.data.text) + '</div>' +
           '<div class="d">' + esc(it.data.detail) + '</div>' +
           '</div>';
  }).join('');
}
function showGovDetailAt(i){
  var it = currentGovItems[i];
  if (!it) return;
  showGovDetail(it.data, it.type);
}
function togglePathList(btn){
  var box = btn.parentNode.nextElementSibling;
  if (!box) return;
  var open = box.style.display === 'none';
  box.style.display = open ? '' : 'none';
  btn.textContent = open ? '收起' : '展开';
}
// 路径块：标题写明库 / 季 / 文件数，并标出这一侧是「将删除」还是「保留」；默认折叠，点按钮展开
function pathListBlock(label, paths, count, tag, season){
  if (!paths || !paths.length) return '';
  var n = (count != null) ? count : paths.length;
  var sn = (season != null) ? ' · 第 ' + season + ' 季' : '';
  var tagHtml = tag ? '<span class="path-tag ' + ((tag === '将删除' || tag === '已删除') ? 'del' : 'keep') + '">' + esc(tag) + '</span>' : '';
  var note = (n > paths.length) ? '<div style="opacity:.7">…仅列出前 ' + paths.length + ' 个，共 ' + n + ' 个</div>' : '';
  return '<div class="meta-row path-block" style="display:block">' +
    '<div class="path-hd"><span class="k">' + esc(label + sn) + ' · ' + n + ' 个文件</span>' + tagHtml +
    '<button class="btn gray path-toggle" onclick="togglePathList(this)">展开</button></div>' +
    '<div class="v path-list" style="display:none">' + paths.map(function(x){ return '<div>' + esc(x) + '</div>'; }).join('') + note + '</div></div>';
}
function recResultRows(ctx){
  var r = ctx && ctx.result;
  if (!r) return '';
  var label = { ok: '✓ 已删除', partial: '⚠ 部分失败（未删成功的已保留）', noop: '未产生变更（文件已不存在或被保护）' }[r.status] || r.status;
  var h = '<div class="meta-row"><span class="k">执行结果</span><span class="v">' + esc(label) + '</span></div>';
  h += '<div class="meta-row"><span class="k">删除 STRM</span><span class="v">' + (r.removed || 0) + ' / ' + (r.planned || 0) + ' 个</span></div>';
  if (r.cloud_removed) h += '<div class="meta-row"><span class="k">云端源文件</span><span class="v">已删除 ' + r.cloud_removed + ' 个</span></div>';
  if (r.sidecars_removed) h += '<div class="meta-row"><span class="k">STRM 库附属文件</span><span class="v">已删除 ' + r.sidecars_removed + ' 个</span></div>';
  if (r.cloud_sidecars_removed) h += '<div class="meta-row"><span class="k">115 附属文件</span><span class="v">已删除 ' + r.cloud_sidecars_removed + ' 个</span></div>';
  var notes = [];
  if (r.cloud_missing)   notes.push(r.cloud_missing + ' 个未找到源');
  if (r.cloud_ambiguous) notes.push(r.cloud_ambiguous + ' 个多候选');
  if (r.cloud_fallback)  notes.push(r.cloud_fallback + ' 个仅兜底');
  if (notes.length) h += '<div class="meta-row"><span class="k">云端提示</span><span class="v">' + esc(notes.join('、')) + '</span></div>';
  (r.errors || []).forEach(function(e){ h += '<div class="meta-row"><span class="k">⚠️ 错误</span><span class="v">' + esc(e) + '</span></div>'; });
  if (ctx.ts) h += '<div class="meta-row"><span class="k">执行时间</span><span class="v">' + esc(ctx.ts) + '</span></div>';
  return h;
}
function showGovDetail(d, type, ctx){
  var meta = d.meta || {};
  var rows = [];
  var done = !!(ctx && ctx.executed);
  rows.push('<div class="meta-row"><span class="k">标题</span><span class="v">' + esc(d.text) + '</span></div>');
  if (done) rows.push(recResultRows(ctx));
  if (d.reason_label) rows.push('<div class="meta-row"><span class="k">原因</span><span class="v">' + esc(d.reason_label) + '</span></div>');
  if (meta.title) rows.push('<div class="meta-row"><span class="k">剧名</span><span class="v">' + esc(meta.title) + '</span></div>');
  if (meta.season != null) rows.push('<div class="meta-row"><span class="k">季</span><span class="v">S' + String(meta.season).padStart(2,'0') + '</span></div>');
  if (meta.local_seasons != null) rows.push('<div class="meta-row"><span class="k">本地季数</span><span class="v">' + esc(meta.local_seasons) + '</span></div>');
  if (meta.share_seasons != null) rows.push('<div class="meta-row"><span class="k">分享季数</span><span class="v">' + esc(meta.share_seasons) + '</span></div>');
  if (meta.compare) rows.push('<div class="meta-row" style="display:block"><div class="k" style="margin-bottom:6px">画质对比依据' + (meta.compare.episode != null ? '（代表集 E' + String(meta.compare.episode).padStart(2,'0') + '）' : '') + '</div>' +
    '<div style="font-size:10px;color:var(--text-dim);word-break:break-all;margin-bottom:6px">分享：' + esc(meta.compare.share_name || '') + '<br>本地：' + esc(meta.compare.local_name || '') + '</div>' + coverCompareHtml(meta.compare) + '</div>');
  if (meta.libs && meta.libs.length) rows.push('<div class="meta-row"><span class="k">所在库</span><span class="v">' + esc(meta.libs.join(' / ')) + '</span></div>');
  if (meta.paths_truncated) rows.push('<div class="meta-row"><span class="k">提示</span><span class="v">文件路径较多，执行记录里仅保留了前 ' + ((meta.local_paths || meta.share_paths || []).length) + ' 个</span></div>');
  if (meta.members && meta.members.length) rows.push('<div class="meta-row" style="display:block"><div class="k" style="margin-bottom:6px">包含 ' + (meta.member_count || meta.members.length) + ' 项</div><div class="ing-tags" style="max-height:220px;overflow-y:auto">' + meta.members.map(function(m){ return '<span>' + esc(m) + '</span>'; }).join('') + '</div></div>');
  if (meta.match_basis) rows.push('<div class="meta-row"><span class="k">匹配来源</span><span class="v">' + esc(meta.match_basis) + '</span></div>');
  if (meta.match_basis_detail) rows.push('<div class="meta-row"><span class="k">匹配依据</span><span class="v">' + esc(meta.match_basis_detail) + '</span></div>');
  var _del = done ? '已删除' : '将删除';
  var _lt = (type === 'loc') ? _del : (type === 'shr' ? '保留' : '');
  var _st = (type === 'shr') ? _del : (type === 'loc' ? '保留' : '');
  rows.push(pathListBlock('本地', meta.local_paths, meta.local_count, _lt, meta.season));
  rows.push(pathListBlock('分享', meta.share_paths, meta.share_count, _st, meta.season));
  if (meta.tmdb && meta.tmdb.length) rows.push('<div class="meta-row"><span class="k">TMDB</span><span class="v">' + esc(meta.tmdb.join(' / ')) + (meta.tmdb_consistent ? '' : ' <b style="color:#f59e0b">⚠ 标识不一致</b>') + '</span></div>');
  if (meta.local_tmdb && meta.local_tmdb.length) rows.push('<div class="meta-row"><span class="k">本地 TMDB</span><span class="v">' + esc(meta.local_tmdb.join(' / ')) + '</span></div>');
  if (meta.share_tmdb && meta.share_tmdb.length) rows.push('<div class="meta-row"><span class="k">分享 TMDB</span><span class="v">' + esc(meta.share_tmdb.join(' / ')) + '</span></div>');
  if (meta.identity_source) rows.push('<div class="meta-row"><span class="k">身份来源</span><span class="v">' + esc(meta.identity_source) + '</span></div>');
  if (meta.keywords) rows.push('<div class="meta-row"><span class="k">命中关键词</span><span class="v">' + esc((meta.keywords || []).join(', ')) + '</span></div>');
  rows.push('<div class="meta-row"><span class="k">涉及文件数</span><span class="v">' + (d.files_count || 0) + '</span></div>');
  var back = (ctx && ctx.backPlan) ? '<button class="btn gray" style="margin-right:8px" onclick="showPlanDetail(&quot;' + esc(ctx.backPlan) + '&quot;)">返回清单</button>' : '';
  var html = '<h3>' + icon('info') + (done ? '执行详情' : '详情') + '</h3>' + rows.join('') +
             '<div style="margin-top:16px;text-align:right">' + back + '<button class="btn gray" onclick="closeModal()">关闭</button></div>';
  openModal(html);
}
async function runClean(){
  if (!currentPlan) return toast('请先扫描双库', 'error');
  if (govPlanExpireAt && Date.now() > govPlanExpireAt){ expireGovPlan(); return toast('清单已过期，请重新扫描', 'error', 4000); }
  if (!(confirm('确认执行清理？将按清单删除文件'))) return;
  if ($('govExecBtn')) $('govExecBtn').disabled = true;
  try {
    var res = await api('/api/clean', { method: 'POST', body: JSON.stringify({ plan_id: currentPlan, dry_run: false }) });
    var t = await pollTask(res.task_id, '执行清理中...');
    if (t.status !== 'success') throw new Error(t.error || '执行失败');
    var lines = (t.result.detail || []).join('\n');
    alert('【执行结果】\n\n' + lines + '\n\n释放本地: ' + t.result.loc_cnt + '  淘汰分享: ' + t.result.sh_cnt);
    currentPlan = null;
    clearGovStateSession();
    loadDashboard();
    // 清理完成后不再自动重扫：扫描要么手动触发，要么由后台定时轮询。
    // 这里只把旧清单清空，并提示用户清单已用完。
    govData = { loc: [], shr: [], keep: [], exempt: [], exemptTotal: 0 };
    ['gov-keep','gov-loc','gov-shr','gov-exempt'].forEach(function(id){ if ($(id)) $(id).textContent = '0'; });
    if ($('govExecBtn')) $('govExecBtn').disabled = true;
    renderGovList();
    setGovBanner('✅ 清理已完成，清单已用完。需要查看最新状态时请点「重新扫描」');
  } catch(e){ toast(e.message, 'error', 4000); }
}

