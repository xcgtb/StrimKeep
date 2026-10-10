/* ═══════════ 治理策略 ═══════════ */
async function loadStrategy(){
  try {
    var r = await api('/api/strategy');
    currentStrategy = r.strategy || currentStrategy;
    applyStrategyUI();
  } catch(e){ console.warn(e); }
}
function applyStrategyUI(){
  document.querySelectorAll('.strategy-option[data-v]').forEach(function(el){
    el.classList.toggle('on', el.dataset.v === currentStrategy.decision);
  });
  document.querySelectorAll('.strategy-option[data-sp]').forEach(function(el){
    el.classList.toggle('on', el.dataset.sp === (currentStrategy.special_action || 'compare'));
  });
  document.querySelectorAll('.strategy-option[data-msp]').forEach(function(el){
    el.classList.toggle('on', el.dataset.msp === (currentStrategy.multi_season_protect || 'compare'));
  });
  document.querySelectorAll('.strategy-option[data-ms]').forEach(function(el){
    el.classList.toggle('on', el.dataset.ms === (currentStrategy.match_strategy || 'title_year'));
  });
  $('strTieLocal').checked = !!currentStrategy.tie_keep_local;
  var rt = Number(currentStrategy.season_replace_ratio);
  $('strRatio').value = Math.round((isFinite(rt) && rt >= 0.5 && rt <= 1 ? rt : 0.9) * 100);
  renderRatio();
  renderExempt();
}
function pickStrategy(v){
  if (v === currentStrategy.decision) return;
  var label = { quality_first:'画质优先', keep_local:'保留本地', keep_share:'保留分享' }[v] || v;
  if (!confirm('确定将决策模型切换为「' + label + '」？\n\n这会影响双库治理的保留策略。')) {
    applyStrategyUI(); return;
  }
  currentStrategy.decision = v;
  applyStrategyUI();
  saveStrategy();
}
function pickSpecial(v){
  if (v === currentStrategy.special_action) return;
  var label = { compare:'画质对比', ignore:'忽略', delete:'清理' }[v] || v;
  if (!confirm('确定将特别篇（S00）策略切换为「' + label + '」？')) {
    applyStrategyUI(); return;
  }
  currentStrategy.special_action = v;
  applyStrategyUI();
  saveStrategy();
}
function pickMultiProtect(v){
  if (v === currentStrategy.multi_season_protect) return;
  var label = { off:'关闭', compare:'开启' }[v] || v;
  if (!confirm('确定将多季合集保护切换为「' + label + '」？\n\n这会直接影响多季合集是否被删。')) {
    applyStrategyUI(); return;
  }
  currentStrategy.multi_season_protect = v;
  applyStrategyUI();
  saveStrategy();
}
function pickMatchStrategy(v){
  if (v === currentStrategy.match_strategy) return;
  var label = { title_year:'剧名+年份（安全）', tmdb_first:'TMDB 优先（高召回）' }[v] || v;
  if (!confirm('确定将配对策略切换为「' + label + '」？\n\n切换后旧治理计划会失效，需重新扫描生成新计划。')) {
    applyStrategyUI(); return;
  }
  currentStrategy.match_strategy = v;
  applyStrategyUI();
  saveStrategy();
}
async function saveStrategy(){
  try {
    var body = {
      decision: currentStrategy.decision,
      match_strategy: currentStrategy.match_strategy || 'title_year',
      multi_season_protect: currentStrategy.multi_season_protect || 'compare',
      tie_keep_local: $('strTieLocal').checked,
      season_replace_ratio: Number($('strRatio').value) / 100,
      special_action: currentStrategy.special_action || 'compare',
      exempt_keywords: currentStrategy.exempt_keywords || [],
    };
    var r = await api('/api/strategy', { method: 'POST', body: JSON.stringify(body) });
    currentStrategy = r.strategy || currentStrategy;
    applyStrategyUI();
    toast('策略已保存', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}

function renderRatio(){
  var pct = Number($('strRatio').value);
  $('ratioVal').textContent = pct + '%';
  var need = Math.ceil(20 * pct / 100 - 1e-9);
  $('ratioDesc').textContent = '逐集比画质：分享在两库共同集里「达标」的占比 ≥ ' + pct +
    '%，且分享集数不少于本地，才删本地腾网盘，否则删分享（例：共同 20 集需 ' + need +
    ' 集达标）。多季保护「开启」档在两边季集合相同时要求每一季都达到；选了「保留本地 / 保留分享」时不使用此阈值。';
}
function onRatioChange(){
  var pct = Number($('strRatio').value);
  if (!confirm('确定将剧集分享达标率阈值设为 ' + pct + '%？\n\n保存后旧治理计划会失效，需重新扫描。')) {
    var rt = Number(currentStrategy.season_replace_ratio);
    $('strRatio').value = Math.round((isFinite(rt) && rt >= 0.5 && rt <= 1 ? rt : 0.9) * 100);
    renderRatio();
    return;
  }
  saveStrategy();
}

function onTieLocalToggle(cb){
  var want = cb.checked;
  if (!confirm('确定将「平局保留本地」切换为「' + (want ? '保留本地' : '保留分享') + '」？')) {
    cb.checked = !want;  // 取消则回滚
    return;
  }
  saveStrategy();
}

/* ═══════════ 画质对比规则编辑器（7 维，对齐上游 TgtoDrive） ═══════════ */
var currentCover = null;
async function loadCoverStrategy(){
  try {
    var r = await api('/api/cover-strategy');
    if (r.status !== 'success') throw new Error(r.message || '加载失败');
    currentCover = r.strategy;
    coverBaseline = coverSnapshot();
    renderCoverRules();
  } catch(e){ var el2 = $('coverRules'); if (el2) el2.innerHTML = '<div class="list-empty">画质对比规则加载失败：' + esc(e.message || e) + '</div>'; }
}
function _iconBtn(txt, fn, dis){
  return '<button class="btn gray" style="min-height:28px;padding:2px 8px' + (dis ? ';opacity:.35' : '') + '" ' + (dis ? 'disabled ' : '') + 'onclick="' + fn + '">' + txt + '</button>';
}
function renderCoverRules(){
  var el = $('coverRules');
  if (!el || !currentCover) return;
  var rules = currentCover.rules || [];
  el.innerHTML = rules.map(function(r, i){
    var k = jsarg(r.key);
    var skipped = false;
    var body = '';
    if (r.key === 'release_group') {
      body = (r.groups || []).map(function(g, gi){
        return '<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid var(--border);border-radius:8px;margin:4px 0">' +
          '<span style="width:20px;color:var(--text-dim);font-size:11px">' + (gi + 1) + '</span>' +
          '<span style="flex:1">' + esc(g) + '</span>' +
          _iconBtn('↑', 'coverMoveGroup(' + gi + ',-1)', gi === 0) + _iconBtn('↓', 'coverMoveGroup(' + gi + ',1)', gi === r.groups.length - 1) +
          _iconBtn('×', 'coverDelGroup(' + gi + ')') + '</div>';
      }).join('') +
      '<div style="display:flex;gap:6px;margin-top:6px"><input id="coverGroupInput" placeholder="新增发布组，例如 CMCTV" style="flex:1">' +
      '<button class="btn gray" onclick="coverAddGroup()">添加</button></div>' +
      '<div style="font-size:10px;color:var(--text-dim);margin-top:4px">发布组取自文件名末尾「-组名」。列表为空时本项不参与比较；不在列表里的排最后。</div>';
    } else {
      body = (r.tiers || []).map(function(t, ti){
        var last = ti === r.tiers.length - 1;
        return '<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid var(--border);border-radius:8px;margin:4px 0">' +
          '<span style="width:20px;color:var(--text-dim);font-size:11px">' + (ti + 1) + '</span>' +
          '<span style="flex:1">' + esc(t) + '</span>' +
          _iconBtn('↑', 'coverMoveTier(' + k + ',' + ti + ',-1)', ti === 0) +
          _iconBtn('↓', 'coverMoveTier(' + k + ',' + ti + ',1)', last) + '</div>';
      }).join('');
    }
    return '<div style="border:1px solid var(--border);border-radius:12px;padding:10px 12px;margin:8px 0' + (r.enabled && !skipped ? '' : ';opacity:.7') + '">' +
      '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
      '<span style="width:22px;color:var(--text-dim);font-weight:700">' + String(i + 1).padStart(2, '0') + '</span>' +
      '<div style="flex:1;min-width:140px"><div style="font-size:13px;font-weight:600">' + esc(r.label || r.key) + '</div>' +
      '<div style="font-size:10px;color:var(--text-dim)">' + esc(r.desc || '') + '</div></div>' +
      '<label style="display:flex;align-items:center;gap:4px;font-size:11px;cursor:pointer"><input type="checkbox" ' + (r.enabled ? 'checked' : '') + ' onchange="coverToggleRule(' + k + ', this.checked)"> ' + (r.enabled ? '已启用' : '已关闭') + '</label>' +
      _iconBtn('↑', 'coverMoveRule(' + k + ',-1)', i === 0) + _iconBtn('↓', 'coverMoveRule(' + k + ',1)', i === rules.length - 1) +
      (skipped ? '' : _iconBtn(r.__open ? '收起' : (r.key === 'release_group' ? '设置列表' : '设置顺序'), 'coverToggleTiers(' + k + ')')) +
      '</div>' + (skipped ? '' : '<div style="' + (r.__open ? '' : 'display:none;') + 'margin-top:8px"><div style="font-size:11px;color:var(--text-dim);margin-bottom:2px">越靠前优先级越高</div>' + body + '</div>') + '</div>';
  }).join('');
  coverUpdateDirty();
}
function _coverFind(key){ return (currentCover.rules || []).filter(function(r){ return r.key === key; })[0]; }
function coverToggleRule(key, on){ _coverFind(key).enabled = on; renderCoverRules(); }
function coverMoveRule(key, dir){
  var rs = currentCover.rules;
  var i = rs.findIndex(function(r){ return r.key === key; });
  var j = i + dir;
  if (i < 0 || j < 0 || j >= rs.length) return;
  var t = rs[i]; rs[i] = rs[j]; rs[j] = t;
  renderCoverRules();
}
function coverToggleTiers(key){ var r = _coverFind(key); r.__open = !r.__open; renderCoverRules(); }
function coverMoveTier(key, ti, dir){
  var r = _coverFind(key);
  var j = ti + dir;
  if (j < 0 || j >= r.tiers.length) return;
  var t = r.tiers[ti]; r.tiers[ti] = r.tiers[j]; r.tiers[j] = t;
  renderCoverRules();
}
function coverMoveGroup(gi, dir){
  var g = _coverFind('release_group').groups, j = gi + dir;
  if (j < 0 || j >= g.length) return;
  var t = g[gi]; g[gi] = g[j]; g[j] = t;
  renderCoverRules();
}
function coverDelGroup(gi){ _coverFind('release_group').groups.splice(gi, 1); renderCoverRules(); }
function coverAddGroup(){
  var inp = $('coverGroupInput'), v = (inp.value || '').trim();
  if (!v) return;
  var r = _coverFind('release_group');
  if (r.groups.indexOf(v) < 0) r.groups.push(v);
  if (r.groups.length === 1) r.enabled = true;
  renderCoverRules();
}
/* ── 草稿 / 差异 / 二次确认 ── */
var coverBaseline = '';
function coverSnapshot(){
  if (!currentCover) return '';
  return JSON.stringify((currentCover.rules || []).map(function(r){
    return { key: r.key, enabled: !!r.enabled, tiers: r.tiers || null, groups: r.groups || null };
  }));
}
function coverIsDirty(){ return !!currentCover && !!coverBaseline && coverSnapshot() !== coverBaseline; }
function coverUpdateDirty(){
  var el = $('coverDirty');
  if (el) el.style.display = coverIsDirty() ? '' : 'none';
}
window.addEventListener('beforeunload', function(e){
  if (coverIsDirty()) { e.preventDefault(); e.returnValue = ''; }
});
function coverDiffLines(){
  var base = [];
  try { base = JSON.parse(coverBaseline || '[]'); } catch(e){}
  var bmap = {}; base.forEach(function(r, i){ r.__i = i; bmap[r.key] = r; });
  var out = [];
  (currentCover.rules || []).forEach(function(r, i){
    var b = bmap[r.key], name = r.label || r.key;
    if (!b) return;
    if (b.__i !== i) out.push('「' + name + '」优先级 第 ' + (b.__i + 1) + ' 位 → 第 ' + (i + 1) + ' 位');
    if (b.enabled !== !!r.enabled) out.push('「' + name + '」' + (r.enabled ? '关闭 → 启用' : '启用 → 关闭'));
    if (JSON.stringify(b.tiers) !== JSON.stringify(r.tiers || null)) out.push('「' + name + '」档位顺序：' + (b.tiers || []).join(' > ') + '  →  ' + (r.tiers || []).join(' > '));
    if (JSON.stringify(b.groups) !== JSON.stringify(r.groups || null)) out.push('「' + name + '」发布组：' + ((b.groups || []).join('、') || '（空）') + '  →  ' + ((r.groups || []).join('、') || '（空）'));
  });
  return out;
}
function _coverConfirmModal(title, bodyHtml, okText, okFn){
  openModal('<h3>' + title + '</h3>' + bodyHtml +
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">' +
    '<button class="btn gray" onclick="closeModal()">取消</button>' +
    '<button class="btn red" id="coverOkBtn" disabled onclick="' + okFn + '">' + okText + '</button></div>');
  // 防误触：确认键 1.5 秒后才可点
  var n = 2, btn = $('coverOkBtn');
  btn.textContent = okText + '（' + n + 's）';
  var t = setInterval(function(){
    n--;
    if (!$('coverOkBtn') || $('coverOkBtn') !== btn) return clearInterval(t);
    if (n <= 0) { clearInterval(t); btn.disabled = false; btn.textContent = okText; }
    else btn.textContent = okText + '（' + n + 's）';
  }, 750);
}
function requestSaveCover(){
  if (!currentCover) return;
  if (!coverIsDirty()) return toast('规则没有改动', 'info');
  var lines = coverDiffLines();
  var list = lines.length ? '<ul style="margin:8px 0 0 18px;font-size:12px;line-height:1.7">' + lines.map(function(l){ return '<li>' + esc(l) + '</li>'; }).join('') + '</ul>' : '';
  _coverConfirmModal('确认保存「画质对比规则」？',
    '<div style="font-size:12px;color:#dc2626">这是关键策略：保存后立即生效（热加载），会改变双库治理中谁胜谁负、删本地还是删分享。已生成的旧治理计划建议重新扫描。</div>' +
    '<div style="font-size:12px;margin-top:10px;font-weight:600">本次改动（' + lines.length + ' 项）</div>' + list,
    '确认保存并生效', 'saveCoverStrategy()');
}
function requestResetCover(){
  _coverConfirmModal('确认恢复默认规则？',
    '<div style="font-size:12px;color:#dc2626">将覆盖当前全部自定义的顺序、开关、档位和发布组列表，并立即生效。</div>',
    '确认恢复默认', 'resetCoverStrategy()');
}
function discardCoverDraft(){
  if (!coverIsDirty()) return toast('没有未保存的改动', 'info');
  if (!confirm('放弃所有未保存的改动，恢复为已保存的规则？')) return;
  loadCoverStrategy();
}
function goCoverRules(){
  var el = $('coverPanel');
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  el.style.transition = 'box-shadow .3s';
  el.style.boxShadow = '0 0 0 3px #dc2626';
  setTimeout(function(){ el.style.boxShadow = ''; }, 1800);
}
async function saveCoverStrategy(){
  closeModal();
  try {
    var payload = JSON.parse(JSON.stringify(currentCover));
    payload.rules.forEach(function(r){ delete r.__open; delete r.label; delete r.desc; });
    var r2 = await api('/api/cover-strategy', { method: 'POST', body: JSON.stringify({ strategy: payload }) });
    if (r2.status !== 'success') throw new Error(r2.message || '保存失败');
    var open = {};
    (currentCover.rules || []).forEach(function(r){ open[r.key] = r.__open; });
    currentCover = r2.strategy;
    currentCover.rules.forEach(function(r){ r.__open = open[r.key]; });
    coverBaseline = coverSnapshot();
    renderCoverRules();
    toast('画质对比规则已保存并生效', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
async function resetCoverStrategy(){
  closeModal();
  try {
    var r2 = await api('/api/cover-strategy', { method: 'POST', body: JSON.stringify({ strategy: { rules: [] } }) });
    if (r2.status !== 'success') throw new Error(r2.message || '重置失败');
    currentCover = r2.strategy;
    coverBaseline = coverSnapshot();
    renderCoverRules();
    toast('已恢复默认规则', 'success');
  } catch(e){ toast('重置失败: ' + e.message, 'error'); }
}
var CMP_VERDICT = { a:'分享胜', b:'本地胜', tie:'打平', skip:'跳过', after:'—' };
function coverCompareHtml(c){
  if (!c) return '';
  var head;
  if (c.result === 0) head = '全部打平 → 按「' + (c.decision !== 'quality_first' ? (c.decision === 'keep_local' ? '保留本地' : '保留分享') + '」决策' : (c.tie_keep_local ? '平局保留本地' : '平局保留分享') + '」开关');
  else head = (c.result > 0 ? '分享版胜' : '本地版胜') + '，决胜项：' + esc((c.dims.filter(function(d){ return d.key === c.decided_by; })[0] || {}).label || c.decided_by);
  if (c.decision && c.decision !== 'quality_first' && c.result !== 0) head += '（注意：当前决策模式为' + (c.decision === 'keep_local' ? '保留本地' : '保留分享') + '，会直接覆盖对比结果）';
  var rows = (c.dims || []).map(function(d){
    var col = d.verdict === 'a' ? '#10b981' : d.verdict === 'b' ? '#f59e0b' : 'var(--text-dim)';
    return '<tr style="opacity:' + (d.verdict === 'skip' || d.verdict === 'after' ? '.5' : '1') + '">' +
      '<td style="padding:3px 6px">' + esc(d.label) + '</td><td style="padding:3px 6px">' + esc(d.a) + '</td><td style="padding:3px 6px">' + esc(d.b) + '</td>' +
      '<td style="padding:3px 6px;color:' + col + ';white-space:nowrap">' + (CMP_VERDICT[d.verdict] || '') + (d.verdict === 'skip' ? '（' + esc(d.reason) + '）' : '') + '</td></tr>';
  }).join('');
  return '<div style="font-size:12px;font-weight:600;margin-bottom:4px">' + head + '</div>' +
    '<div style="overflow-x:auto"><table style="width:100%;font-size:11px;border-collapse:collapse"><thead><tr style="color:var(--text-dim);text-align:left"><th style="padding:3px 6px">维度</th><th style="padding:3px 6px">分享版</th><th style="padding:3px 6px">本地版</th><th style="padding:3px 6px">结果</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}
async function runCoverCompare(){
  var a = ($('cmpShare').value || '').trim(), b = ($('cmpLocal').value || '').trim();
  if (!a || !b) return toast('请填写两个文件名', 'error');
  try {
    var r = await api('/api/cover-compare', { method: 'POST', body: JSON.stringify({ a: a, b: b }) });
    $('cmpResult').innerHTML = coverCompareHtml(r.compare);
  } catch(e){ toast('对比失败: ' + e.message, 'error'); }
}

/* ═══════════ 白名单 ═══════════ */
function renderExempt(){
  var kws = currentStrategy.exempt_keywords || [];
  $('exemptList').innerHTML = kws.length
    ? kws.map(function(k){
        return '<span class="tag">' + esc(k) + '<span class="rm" data-kw="' + esc(k) + '">×</span></span>';
      }).join('')
    : '<span style="color:#9ca3af;font-size:12px">暂无关键词</span>';
}
function addExempt(){
  var v = $('exemptInput').value.trim();
  if (!v) return;
  var kws = currentStrategy.exempt_keywords || [];
  if (kws.indexOf(v) < 0) kws.push(v);
  currentStrategy.exempt_keywords = kws;
  $('exemptInput').value = '';
  saveStrategyExempt();
}
function removeExempt(k){
  var kws = currentStrategy.exempt_keywords || [];
  var i = kws.indexOf(k);
  if (i >= 0) kws.splice(i, 1);
  currentStrategy.exempt_keywords = kws;
  saveStrategyExempt();
}
async function saveStrategyExempt(){
  try {
    await api('/api/strategy', { method: 'POST', body: JSON.stringify({ exempt_keywords: currentStrategy.exempt_keywords }) });
    renderExempt();
    toast('白名单已更新', 'success');
    scanExempt();
  } catch(e){ toast(e.message, 'error'); }
}
async function scanExempt(){
  $('exemptMatches').innerHTML = '<div class="list-empty">扫描中...</div>';
  try {
    var r = await api('/api/exempt/scan');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    var m = r.matches || [];
    if (!m.length) {
      $('exemptMatches').innerHTML = '<div class="list-empty">双库中未命中白名单</div>';
    } else {
      $('exemptMatches').innerHTML = m.map(function(x){
        var seasons = (x.seasons || []).map(function(s){
          return s === 0 ? 'S00(特别篇)' : 'S' + String(s).padStart(2,'0');
        }).join(', ');
        var n = x.member_count || 0;
        var more = n > 1
          ? '<details class="ing-more"><summary>包含 ' + n + ' 项</summary><div class="ing-tags">'
            + (x.members || []).map(function(m){ return '<span>' + esc(m) + '</span>'; }).join('')
            + (n > (x.members || []).length ? '<span>…</span>' : '') + '</div></details>'
          : '';
        return '<div class="list-item exempt">'
          + '<div class="t">🛡️ 《' + esc(x.title) + '》</div>'
          + '<div class="d">命中「' + esc(x.keyword) + '」 · 库: ' + esc((x.libs || []).join('/')) + (seasons ? ' · 季: ' + seasons : '')
          + (x.strm_count ? ' · ' + esc(String(x.strm_count)) + ' 个文件' : '') + '</div>' + more
          + '</div>';
      }).join('');
    }
  } catch(e){ $('exemptMatches').innerHTML = '<div class="list-empty">扫描失败: ' + esc(e.message) + '</div>'; }
}

/* ═══════════ 入库监控 ═══════════ */
async function loadIngest(){
  try {
    var r = await api('/api/ingest/settings');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    var s = r.settings || {};
    $('ingEnabled').checked = !!s.enabled;
    $('ingInterval').value = s.interval_min || 5;
    // 显示缓存状态
    try {
      var st = await api('/api/ingest/status');
      if (st.has_cache) {
        var age = st.age_sec;
        var ageStr = age < 60 ? age + ' 秒前' : (age < 3600 ? Math.floor(age/60) + ' 分钟前' : Math.floor(age/3600) + ' 小时前');
        var stats = st.stats || {};
        $('ingStatus').textContent = '📦 缓存于 ' + ageStr + ' · 电影 ' + (stats.movies||0) + ' / 剧集 ' + (stats.series||0) + ' 部 / ' + (stats.episodes||0) + ' 集';
      } else {
        $('ingStatus').textContent = '⚪ 暂无缓存，等待后台首次刷新';
      }
    } catch(e){ $('ingStatus').textContent = ''; }
  } catch(e){ toast('加载入库设置失败: ' + e.message, 'error'); }
}
async function saveIngest(){
  try {
    await api('/api/ingest/settings', { method: 'POST', body: JSON.stringify({
      enabled: $('ingEnabled').checked,
      interval_min: parseInt($('ingInterval').value) || 5,
    })});
    toast('已保存', 'success');
    loadIngest();
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}

/* ═══════════ 双库定时巡检 ═══════════ */
function _fmtGovTime(ts){
  if (!ts) return '—';
  var d = new Date(ts * 1000);
  var p = function(n){ return (n < 10 ? '0' : '') + n; };
  return (d.getMonth()+1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
async function loadGovAuto(){
  try {
    var r = await api('/api/governance/auto');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    var s = r.settings || {};
    $('govAutoEnabled').checked = !!s.enabled;
    $('govAutoInterval').value = s.interval_hours || 6;
    $('govAutoOnChange').checked = !!s.only_on_change;
    $('govAutoNotifyClean').checked = !!s.notify_when_clean;
    var line = s.running ? '⏳ 正在巡检中...' : (s.last_run
      ? '🕒 上次巡检 ' + _fmtGovTime(s.last_run) + (s.last_status ? ' · ' + s.last_status : '')
      : '⚪ 尚未巡检');
    if (s.enabled && !s.running) line += s.next_run ? ' · 下次约 ' + _fmtGovTime(s.next_run) : ' · 即将开始首次巡检';
    $('govAutoStatus').textContent = line;
  } catch(e){ toast('加载巡检设置失败: ' + e.message, 'error'); }
}
async function saveGovAuto(){
  try {
    await api('/api/governance/auto', { method: 'POST', body: JSON.stringify({
      enabled: $('govAutoEnabled').checked,
      interval_hours: parseInt($('govAutoInterval').value) || 6,
      only_on_change: $('govAutoOnChange').checked,
      notify_when_clean: $('govAutoNotifyClean').checked,
    })});
    toast('已保存', 'success');
    loadGovAuto();
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}

/* ═══════════ 服务配置 ═══════════ */
async function loadConfig(){
  try {
    revealed = {};
    var r = await api('/api/config');
    var c = r.config || {};
    $('cfg-emby-host').value = c.emby_host || '';
    $('cfg-emby-key').value  = c.emby_key || '';
    $('cfg-emby-local-path').value = c.emby_local_path || '';
    $('cfg-emby-share-path').value = c.emby_share_path || '';
    $('cfg-tmdb-key').value  = c.tmdb_key || '';
    $('cfg-proxy-enabled').checked = c.http_proxy_enabled === '1';
    $('cfg-proxy-url').value = c.http_proxy_url || '';
    $('cfg-proxy-user').value = c.http_proxy_username || '';
    $('cfg-proxy-password').value = c.http_proxy_password || '';
    $('proxyTestResult').textContent = '';
    $('cfg-tg-token').value  = c.telegram_bot_token || '';
    $('cfg-tg-chat').value   = c.telegram_chat_id || '';
    $('cfg-tg-users').value  = c.telegram_allowed_users || '';
    ['emby_key','tmdb_key','telegram_bot_token'].forEach(function(k){
      var btn = document.querySelector('.eye-btn[data-eye="' + k + '"]');
      if (btn) { btn.classList.remove('on'); btn.innerHTML = icon('eye') + '<span>查看</span>'; }
      var row = document.getElementById('row-' + k.replace('_key','-key').replace('telegram_bot_token','tg-token'));
      if (row) row.classList.remove('revealed');
    });
  } catch(e){ toast('加载配置失败: ' + e.message, 'error', 4000); }
}
async function toggleReveal(key){
  var btn = document.querySelector('.eye-btn[data-eye="' + key + '"]');
  var inputId = { emby_key:'cfg-emby-key', tmdb_key:'cfg-tmdb-key', telegram_bot_token:'cfg-tg-token' }[key];
  var rowId = { emby_key:'row-emby-key', tmdb_key:'row-tmdb-key', telegram_bot_token:'row-tg-token' }[key];
  var input = $(inputId);
  if (!input) return;
  if (revealed[key]) {
    revealed[key] = false;
    try { var r = await api('/api/config'); input.value = (r.config || {})[key] || ''; } catch(e){}
    if (btn) { btn.classList.remove('on'); btn.innerHTML = icon('eye') + '<span>查看</span>'; }
    if (rowId && $(rowId)) $(rowId).classList.remove('revealed');
  } else {
    if (!(confirm('⚠️ 即将显示完整密钥，请确保周围环境安全。继续？'))) return;
    try {
      var r = await api('/api/config?reveal=1');
      input.value = (r.config || {})[key] || '';
      revealed[key] = true;
      if (btn) { btn.classList.add('on'); btn.innerHTML = icon('eye-off') + '<span>隐藏</span>'; }
      if (rowId && $(rowId)) $(rowId).classList.add('revealed');
      toast('已显示，60 秒后自动重新脱敏', 'success');
      setTimeout(function(){ if (revealed[key]) toggleReveal(key); }, 60000);
    } catch(e){ toast('加载失败: ' + e.message, 'error'); }
  }
}
function botErrText(e){
  if (!e) return '';
  if (/timed out/i.test(e)) return '连接 Telegram 超时（网络波动，自动重试中）';
  if (/401|Unauthorized/i.test(e)) return 'Bot Token 无效（401）';
  if (/409|Conflict/i.test(e)) return '同一个 Bot Token 正被另一个实例使用（409）';
  return e;
}
async function loadBotStatus(){
  try {
    var r = await api('/api/bot/status');
    var b = r.bot || {};
    var st = b.state || (b.running ? 'ok' : 'stopped');
    var map = {
      ok:       ['on',   'Bot 运行中'],
      starting: ['warn', 'Bot 启动中'],
      degraded: ['warn', 'Bot 网络不稳定，正在重试'],
      down:     ['bad',  'Bot 无响应'],
      stopped:  ['off',  'Bot 未运行']
    };
    var m = map[st] || map.stopped;
    var uname = b.bot_username ? ' @' + b.bot_username : '';
    var lastAgo = b.last_poll_ago != null ? (b.last_poll_ago + ' 秒前') : '—';
    var detail = '上次轮询: ' + lastAgo;
    if (st === 'ok') detail += ' · 轮询正常';
    // 只在「当前仍在失败」时显示错误；恢复后后端会清空，不会再残留旧报错
    if (b.last_error) {
      detail += '<br>⚠️ ' + botErrText(b.last_error).replace(/</g, '&lt;') +
                (b.fail_count > 1 ? '（连续 ' + b.fail_count + ' 次）' : '');
    }
    $('botStatus').innerHTML =
      '<div class="bot-status"><span class="dot ' + m[0] + '"></span>' + m[1] + esc(uname) + '</div>' +
      '<div style="margin-top:6px;color:#9ca3af">' + detail + '</div>';
  } catch(e){ $('botStatus').textContent = '加载失败: ' + e.message; }
}
// 设置页可见时每 8 秒自动刷新一次，不用再手动刷新页面
setInterval(function(){
  var el = $('botStatus');
  if (el && el.offsetParent !== null) loadBotStatus();
}, 8000);
async function saveConfig(){
  var body = {
    emby_host: $('cfg-emby-host').value.trim(),
    emby_key:  $('cfg-emby-key').value.trim(),
    emby_local_path: $('cfg-emby-local-path').value.trim(),
    emby_share_path: $('cfg-emby-share-path').value.trim(),
    tmdb_key:  $('cfg-tmdb-key').value.trim(),
    telegram_bot_token:     $('cfg-tg-token').value.trim(),
    telegram_chat_id:       $('cfg-tg-chat').value.trim(),
    telegram_allowed_users: $('cfg-tg-users').value.trim(),
  };
  Object.assign(body, proxyConfigBody());
  try {
    var r = await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
    if (r.status === 'success') {
      toast('配置已保存并生效', 'success');
      loadDashboard(); embyLoaded = false; loadConfig();
      setTimeout(loadBotStatus, 1500);
    } else toast('保存失败: ' + (r.message || ''), 'error', 4000);
  } catch(e){ toast(e.message, 'error', 4000); }
}
async function testEmby(){
  toast('测试中...');
  try {
    var r = await api('/api/config/test/emby', { method: 'POST', body: JSON.stringify({ emby_host: $('cfg-emby-host').value.trim(), emby_key: $('cfg-emby-key').value.trim(), emby_local_path: $('cfg-emby-local-path').value.trim(), emby_share_path: $('cfg-emby-share-path').value.trim() }) });
    alert(r.message);
  } catch(e){ alert(e.message); }
}
async function testTmdb(){
  toast('测试中...');
  try {
    var r = await api('/api/config/test/tmdb', { method: 'POST', body: JSON.stringify(Object.assign({ tmdb_key: $('cfg-tmdb-key').value.trim() }, proxyConfigBody())) });
    alert(r.message);
  } catch(e){ alert(e.message); }
}
async function testTelegram(){
  toast('发送中...');
  try {
    var r = await api('/api/config/test/telegram', { method: 'POST', body: JSON.stringify(Object.assign({ telegram_bot_token: $('cfg-tg-token').value.trim(), telegram_chat_id: $('cfg-tg-chat').value.trim() }, proxyConfigBody())) });
    alert(r.message);
  } catch(e){ alert(e.message); }
}

function proxyConfigBody(){
  return {http_proxy_enabled:$('cfg-proxy-enabled').checked ? '1' : '0',
    http_proxy_url:$('cfg-proxy-url').value.trim(),
    http_proxy_username:$('cfg-proxy-user').value.trim(),
    http_proxy_password:$('cfg-proxy-password').value};
}
async function testHttpProxy(){
  var button=$('proxyTestBtn'), result=$('proxyTestResult');
  button.disabled=true;result.textContent='正在通过代理连接 TMDB…';
  try {
    var r=await api('/api/config/test/proxy',{method:'POST',body:JSON.stringify(proxyConfigBody()),timeoutMs:13000});
    result.textContent=r.message || '测试完成';
    result.style.color=r.status==='success' ? 'var(--ok)' : 'var(--warn)';
  } catch(e){result.textContent=e.message;result.style.color='var(--warn)';}
  finally{button.disabled=false;}
}

