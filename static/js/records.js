/* ═══════════ 每日汇报 ═══════════ */

function toggleRecordDetails(idx, showCount){
  var box = document.getElementById('rec-details-' + idx);
  var btn = document.getElementById('rec-toggle-' + idx);
  if (!box || !btn || !window.__recCache) return;
  var all = window.__recCache[idx] || [];
  var expanded = btn.dataset.expanded === '1';
  if (expanded) {
    box.innerHTML = all.slice(0, showCount).map(function(d){ return '<div>' + esc(d) + '</div>'; }).join('');
    btn.textContent = '展开全部 ' + all.length + ' 条 ▾';
    btn.dataset.expanded = '0';
  } else {
    box.innerHTML = all.map(function(d){ return '<div>' + esc(d) + '</div>'; }).join('');
    btn.textContent = '收起 ▴';
    btn.dataset.expanded = '1';
  }
}

/* ═══════════ 执行记录（结构化渲染） ═══════════ */
var REC_DETAIL_RE = /^[\s├└│─┬┌]*(?:(\S+)\s+)?《(.+?)》\s*(S\d{1,3})?\s*[:：]\s*(.*?)\s*(?:→|➔|->)\s*(.+)$/;
function recSeasonRange(list){
  var nums = list.map(function(s){ return parseInt(s.slice(1), 10); }).filter(function(n){ return !isNaN(n); })
    .sort(function(a, b){ return a - b; });
  if (!nums.length) return '';
  var out = [], st = nums[0], pv = nums[0];
  function pad(n){ return 'S' + String(n).padStart(2, '0'); }
  for (var i = 1; i <= nums.length; i++) {
    if (nums[i] === pv + 1) { pv = nums[i]; continue; }
    out.push(st === pv ? pad(st) : pad(st) + '–' + pad(pv));
    st = pv = nums[i];
  }
  return out.join('、');
}
function recGroupDetails(details){
  var groups = [], index = {}, plain = [];
  details.forEach(function(line){
    var m = REC_DETAIL_RE.exec(line);
    if (!m) { if (String(line).trim()) plain.push(String(line).trim()); return; }
    var key = m[2] + '|' + m[4] + '|' + m[5];
    var g = index[key];
    if (!g) { g = index[key] = { icon: m[1] || '', title: m[2], why: m[4], act: m[5], seasons: [] }; groups.push(g); }
    if (m[3]) g.seasons.push(m[3]);
  });
  return { groups: groups, plain: plain };
}
function recSummary(title){
  var rest = String(title || ''), lead = '', emby = null;
  var m = rest.match(/^(.{1,14}?)[：:]\s*(.+)$/);
  if (m) { lead = m[1]; rest = m[2]; }
  rest = rest.replace(/\s*[（(]\s*Emby\s*刷新\s*[:：]\s*(True|False)\s*[）)]\s*$/i, function(_, v){ emby = /true/i.test(v); return ''; });
  var chips = [], ok = true;
  rest.split(/[,，]\s*/).forEach(function(p){
    var mm = p.trim().match(/^(.+?)\s*(\d+)\s*项$/);
    if (mm) chips.push({ k: mm[1], v: mm[2] }); else ok = false;
  });
  if (!ok || !chips.length) return null;
  return { lead: lead, chips: chips, emby: emby };
}
function recTone(cat){
  if (cat.indexOf('清理') >= 0) return 'clean';
  if (cat.indexOf('巡检') >= 0 || cat.indexOf('扫描') >= 0) return 'scan';
  if (cat.indexOf('配置') >= 0) return 'config';
  if (cat.indexOf('搜片') >= 0) return 'search';
  return 'other';
}
function recDayLabel(day){
  var d = new Date(day.replace(/-/g, '/') + ' 00:00:00');
  if (isNaN(d.getTime())) return day;
  var wk = '日一二三四五六'.charAt(d.getDay());
  var t = new Date(); t.setHours(0, 0, 0, 0);
  var diff = Math.round((t - d) / 86400000);
  var tag = diff === 0 ? '今天 · ' : (diff === 1 ? '昨天 · ' : '');
  return tag + day.slice(5) + ' 周' + wk;
}
/* 执行记录卡片：样式对齐「入库汇报」——顶部大数字胶囊 + 按动作分组的可折叠清单；
   每一项点开就是和治理清单一模一样的详情（画质对比 / 匹配依据 / 文件路径），多了「执行结果」。 */
var __recs = [];          // 当前渲染的记录（按下标引用，避免把数据塞进 inline JS）
var __recFull = {};       // id → 完整记录（含路径等重字段），点开详情时按需拉取
var REC_GROUP_OPEN_MAX = 5;   // 组内条目 ≤ 此值默认展开，否则折叠
var REC_ROW_MAX = 20;         // 组内一次最多直接列出的条数，其余收进「展开其余」
function recKindMeta(kind){
  return kind === 'loc' ? { src: '本地', cat: '释放本地', hint: '删除本地副本' }
                        : { src: '分享', cat: '淘汰分享', hint: '删除分享副本' };
}
function recResultBadge(res){
  if (!res) return '';
  if (res.status === 'ok')      return '<span class="rs ok">✓ 已删 ' + (res.removed || 0) + '</span>';
  if (res.status === 'partial') return '<span class="rs warn">⚠ 部分失败</span>';
  if (res.status === 'noop')    return '<span class="rs dim">未产生变更</span>';
  return '';
}
function recItemRow(ri, ii, it){
  var s = it.season != null ? '<span class="rd-s">S' + String(it.season).padStart(2, '0') + '</span>' : '';
  var title = it.title || it.text || '';
  return '<li class="clk" onclick="showRecItem(' + ri + ',' + ii + ')">'
    + '<span class="n">《' + esc(title) + '》' + s + recResultBadge(it.result) + '</span>'
    + '<span class="ing-season">' + esc(it.reason_label || it.detail || '') + '</span></li>';
}
function recGroupHtml(ri, kind, entries){
  var m = recKindMeta(kind), n = entries.length;
  var rows = entries.map(function(e){ return recItemRow(ri, e.i, e.it); });
  var body = '<ul class="ing-list">' + rows.slice(0, REC_ROW_MAX).join('') + '</ul>';
  if (rows.length > REC_ROW_MAX) {
    body += '<details class="ing-more rc-more"><summary>展开其余 ' + (rows.length - REC_ROW_MAX) + ' 项 ▾</summary>'
          + '<ul class="ing-list">' + rows.slice(REC_ROW_MAX).join('') + '</ul></details>';
  }
  return '<details class="ing-grp"' + (n <= REC_GROUP_OPEN_MAX ? ' open' : '') + '><summary>'
    + '<span class="ing-src">' + m.src + '</span><span class="ing-cat">' + m.cat + ' · ' + m.hint + '</span>'
    + '<span class="ing-cnt">' + n + ' 项</span></summary>' + body + '</details>';
}
/* 老记录（没有结构化明细）：从文本行里还原「《剧名》 S05 (原因 → 动作)」，仍按动作分组展示，只是不能点开 */
var REC_LEGACY_RE = /^[\s├└│─┬┌]*(?:(\S+)\s+)?《(.+?)》\s*(S\d{1,3})?\s*[（(](.*?)\s*(?:→|➔|->)\s*(.+?)[）)]\s*(?:⚠️.*)?$/;
function recLegacyGroups(details){
  var groups = [], index = {}, rest = [];
  details.forEach(function(line){
    var m = REC_LEGACY_RE.exec(line);
    if (!m) { if (String(line).trim()) rest.push(String(line).trim()); return; }
    var act = m[5].trim(), g = index[act];
    if (!g) { g = index[act] = { act: act, src: act.indexOf('分享') >= 0 ? '分享' : '本地', rows: [] }; groups.push(g); }
    g.rows.push({ title: m[2], season: m[3] || '', why: m[4].trim() });
  });
  return { groups: groups, rest: rest };
}
function recLegacyHtml(det){
  var g = recLegacyGroups(det);
  if (!g.groups.length) return '';
  var h = '';
  g.groups.forEach(function(x){
    var rows = x.rows.map(function(r){
      return '<li><span class="n">《' + esc(r.title) + '》' + (r.season ? '<span class="rd-s">' + esc(r.season) + '</span>' : '') + '</span>'
           + '<span class="ing-season">' + esc(r.why) + '</span></li>';
    });
    var body = '<ul class="ing-list">' + rows.slice(0, REC_ROW_MAX).join('') + '</ul>';
    if (rows.length > REC_ROW_MAX) body += '<details class="ing-more rc-more"><summary>展开其余 ' + (rows.length - REC_ROW_MAX) + ' 项 ▾</summary><ul class="ing-list">' + rows.slice(REC_ROW_MAX).join('') + '</ul></details>';
    h += '<details class="ing-grp"' + (rows.length <= REC_GROUP_OPEN_MAX ? ' open' : '') + '><summary><span class="ing-src">' + x.src + '</span>'
       + '<span class="ing-cat">' + esc(x.act) + '</span><span class="ing-cnt">' + rows.length + ' 项</span></summary>' + body + '</details>';
  });
  g.rest.forEach(function(l){ h += '<div class="rd-plain">' + esc(l) + '</div>'; });
  return h;
}
function recChipsHtml(lines){
  return '<div class="rc-sum">' + lines.map(function(l){
    var on = /(变化|失败|警告|错误)/.test(l) && /[1-9]/.test(l);
    return '<span class="rc-chip' + (on ? ' on' : '') + '">' + esc(l) + '</span>';
  }).join('') + '</div>';
}
/* 追更记录：胶囊 + 按「有变化 / 无变化」分组的订阅清单，每部剧写明新增 / 缺集 / 补齐的集号 */
function recSubRow(x){
  var tags = '';
  if (x.new)           tags += '<span class="rs ok">🆕 新增 ' + esc(x.new) + '</span>';
  if (x.refilled)      tags += '<span class="rs ok">✅ 补齐 ' + esc(x.refilled) + '</span>';
  if (x.newly_missing) tags += '<span class="rs warn">⚠ 缺集 ' + esc(x.newly_missing) + '</span>';
  var info = [];
  if (x.latest_ep) info.push('已通知至 ' + x.latest_ep);
  var tot = x.tmdb_declared || x.tmdb_total;   /* 与订阅卡片同口径：优先 TMDB 标称总集数 */
  if (x.have != null) info.push('本次读取 ' + x.have + (tot != null ? ' / ' + tot : '') + ' 集');
  if (x.error) info.push('失败/不完整：' + x.error);
  if (x.fallback_reason) info.push('Emby 失败，改用完整磁盘事实：' + x.fallback_reason);
  if (x.facts_source) info.push('来源 ' + x.facts_source + (x.facts_ts ? ' · ' + new Date(Number(x.facts_ts)*1000).toLocaleString() : ''));
  if (x.tmdb_source) info.push('TMDB 来源 ' + x.tmdb_source + (x.tmdb_ts ? ' · ' + new Date(Number(x.tmdb_ts)*1000).toLocaleString() : ''));
  if (!x.changed && x.missing) info.push('仍缺 ' + x.missing + ' 集' + (x.missing_eps ? '：' + x.missing_eps : ''));
  return '<li class="stk"><span class="n">《' + esc(x.name || '') + '》' + (tags ? '<span class="rc-tags">' + tags + '</span>' : '') + '</span>'
       + '<span class="ing-season">' + esc(info.join(' · ')) + '</span></li>';
}
function recSubsHtml(ex){
  var shows = ex.shows || [];
  if (!shows.length) return '';
  var bad = shows.filter(function(x){return x.status === 'error' || x.status === 'partial';});
  var good = shows.filter(function(x){return x.status !== 'error' && x.status !== 'partial';});
  var ch = good.filter(function(x){ return x.changed; }), keep = good.filter(function(x){ return !x.changed; });
  function grp(label, tag, list, open){
    if (!list.length) return '';
    return '<details class="ing-grp"' + (open ? ' open' : '') + '><summary><span class="ing-src">' + tag + '</span>'
      + '<span class="ing-cat">' + label + '</span><span class="ing-cnt">' + list.length + ' 部</span></summary>'
      + '<ul class="ing-list">' + list.map(recSubRow).join('') + '</ul></details>';
  }
  return grp('失败/不完整', '检查', bad, true) + grp('有变化', '更新', ch, true) + grp('无变化', '订阅', keep, keep.length <= REC_GROUP_OPEN_MAX && !ch.length && !bad.length);
}
function recCard(rec){
  var cat = rec.category || '', title = rec.title || '';
  ['库间查重巡检', '执行跨库清理', '模糊搜片'].forEach(function(p){
    if (!cat && title.indexOf(p) === 0) { cat = p; title = title.slice(p.length); }
  });
  var ri = __recs.push(rec) - 1;
  var ts = String(rec.ts || ''), time = ts.length >= 19 ? ts.slice(11, 19) : ts;
  var ex = rec.extra || {}, items = ex.items || [], det = rec.details || [];
  var h = '<div class="rc rc-' + recTone(cat) + '"><div class="rc-top"><span class="rc-tag">' + esc(cat || '记录') + '</span><span class="rc-time">' + esc(time) + (rec.rule_sig ? ' · 规则 ' + esc(rec.rule_sig) : '') + '</span></div>';

  /* ① 带结构化明细的「执行跨库清理」：入库汇报同款胶囊 + 分组清单 */
  if (items.length) {
    var nl = ex.n_loc != null ? ex.n_loc : items.filter(function(x){ return x.kind === 'loc'; }).length;
    var ns = ex.n_shr != null ? ex.n_shr : items.filter(function(x){ return x.kind === 'shr'; }).length;
    h += '<div class="ing-sum rc-ing-sum">'
      + '<span class="ing-chip"><b>' + nl + '</b> 项释放本地</span>'
      + '<span class="ing-chip"><b>' + ns + '</b> 项淘汰分享</span>'
      + '<span class="ing-chip">Emby 刷新 ' + (ex.refreshed ? '✅' : '❌') + '</span>'
      + (ex.skipped ? '<span class="ing-chip">跳过 <b>' + ex.skipped + '</b></span>' : '')
      + (ex.warnings ? '<span class="ing-chip warn">⚠ 警告 <b>' + ex.warnings + '</b></span>' : '')
      + '</div>';
    var byKind = { loc: [], shr: [] };
    items.forEach(function(it, i){ (byKind[it.kind === 'loc' ? 'loc' : 'shr']).push({ i: i, it: it }); });
    ['loc', 'shr'].forEach(function(k){ if (byKind[k].length) h += recGroupHtml(ri, k, byKind[k]); });
    if (ex.items_truncated) h += '<div class="rd-plain">清单较长，仅保留前 ' + items.length + ' 项明细。</div>';
    var extra = det.filter(function(l){ return /^(├─|│)/.test(l) || /^⚠️/.test(l); });
    if (extra.length) {
      var rid0 = 'rec-detail-' + Math.random().toString(36).slice(2, 9);
      h += '<div class="rc-det"><button type="button" class="rc-det-btn" aria-expanded="false" data-detail-id="' + rid0 + '" onclick="toggleExecutionDetail(this)">▶ 执行提示 · ' + extra.length + ' 条</button>'
         + '<div id="' + rid0 + '" class="rc-det-body" hidden>' + extra.map(function(l){ return '<div class="rd-plain">' + esc(l) + '</div>'; }).join('') + '</div></div>';
    }
    return h + '</div>';
  }

  /* ② 一般摘要（xx N 项, ...）→ 胶囊 */
  var sum = recSummary(title);
  if (sum) {
    h += '<div class="rc-sum">' + (sum.lead ? '<span class="rc-lead">' + esc(sum.lead) + '</span>' : '');
    sum.chips.forEach(function(c){ h += '<span class="rc-chip' + (c.v !== '0' ? ' on' : '') + '">' + esc(c.k) + ' <b>' + esc(c.v) + '</b></span>'; });
    if (sum.emby !== null) h += '<span class="rc-chip">Emby 刷新 ' + (sum.emby ? '✅' : '❌') + '</span>';
    h += '</div>';
  } else {
    h += '<div class="rc-title">' + esc(title) + '</div>';
  }

  /* 扫描记录：带上治理计划号，可直接回看当时的清单 */
  if (ex.plan_id && cat.indexOf('巡检') >= 0) {
    h += '<div class="rc-act"><button type="button" class="btn sm gray" onclick="showPlanDetail(&quot;' + esc(ex.plan_id) + '&quot;)">查看治理清单</button></div>';
  }

  /* 追更：带逐部订阅结果的新记录 */
  if (ex.shows && ex.shows.length) {
    return h + (det.length ? recChipsHtml(det) : '') + recSubsHtml(ex) + '</div>';
  }

  /* ③ 明细：短的「检查 N 部 / 发现 N 部」直接显示成胶囊，不再套一层折叠 */
  if (det.length) {
    if (det.length <= 3 && det.every(function(l){ return String(l).length <= 40 && !/《/.test(l); })) {
      return h + recChipsHtml(det) + '</div>';
    }
    var legacy = recLegacyHtml(det), body = '', label;
    if (legacy) {
      body = legacy; label = '明细 · ' + det.length + ' 条';
    } else {
      var g = recGroupDetails(det);
      g.groups.forEach(function(x){
        var ss = recSeasonRange(x.seasons);
        body += '<div class="rd-row"><div class="rd-main"><span class="rd-t">《' + esc(x.title) + '》</span>' + (ss ? '<span class="rd-s">' + esc(ss) + '</span>' : '') + '</div>'
              + '<div class="rd-why">' + esc(x.why) + ' → ' + esc(x.act) + '</div></div>';
      });
      g.plain.forEach(function(l){ body += '<div class="rd-plain">' + esc(l) + '</div>'; });
      label = g.groups.length ? ('明细 · ' + g.groups.length + ' 部 / ' + det.length + ' 条') : ('明细 · ' + det.length + ' 条');
    }
    var rid = 'rec-detail-' + Math.random().toString(36).slice(2, 9);
    h += '<div class="rc-det">'
      + '<button type="button" class="rc-det-btn" aria-expanded="false" data-detail-id="' + rid + '" onclick="toggleExecutionDetail(this)">▶ ' + label + '</button>'
      + '<div id="' + rid + '" class="rc-det-body" hidden>' + body + '</div>'
      + '</div>';
  }
  return h + '</div>';
}
/* 点开执行记录里的某一项：和治理清单同一个详情弹窗，多一块「执行结果」 */
async function showRecItem(ri, ii){
  var rec = __recs[ri];
  if (!rec || !rec.extra || !rec.extra.items) return;
  var it = rec.extra.items[ii];
  if (!it) return;
  var m = it.meta || {};
  if (rec.id && (m.has_share_paths || m.has_local_paths || m.has_members)) {
    try {
      if (!__recFull[rec.id]) {
        var r = await api('/api/records/' + rec.id);
        __recFull[rec.id] = (r.record || {});
      }
      var full = ((__recFull[rec.id].extra || {}).items || [])[ii];
      if (full) it = full;
    } catch(e){ toast('完整明细加载失败: ' + e.message, 'error'); }
  }
  showGovDetail({ text: it.text, detail: it.detail, reason_label: it.reason_label, meta: it.meta || {},
                  files_count: it.files_count }, it.kind,
                { executed: true, result: it.result, ts: rec.ts });
}

function toggleExecutionDetail(btn){
  var id = btn && btn.getAttribute('data-detail-id');
  var box = id ? document.getElementById(id) : null;
  if (!box || !btn) return;
  var open = btn.getAttribute('aria-expanded') === 'true';
  box.hidden = open;
  btn.setAttribute('aria-expanded', open ? 'false' : 'true');
  var text = btn.textContent.replace(/^[▶▼]\s*/, '');
  btn.textContent = (open ? '▶ ' : '▼ ') + text;
}

var logCenter = { cursor: 0, timer: null, busy: false, paused: false, generation: 0, ids: new Set() };
function stopLogCenter(){
  logCenter.generation++;
  clearTimeout(logCenter.timer);
  logCenter.timer = null;
}
function toggleLogPause(){
  logCenter.paused = !logCenter.paused;
  $('logPause').textContent = logCenter.paused ? '恢复自动刷新' : '暂停自动刷新';
  if (logCenter.paused) stopLogCenter();
  else loadRecords();
}
function scheduleLogPoll(){
  clearTimeout(logCenter.timer);
  if (window.__activeTab === 'history' && !logCenter.paused && !document.hidden)
    logCenter.timer = setTimeout(pollLogCenter, 2000);
}
async function pollLogCenter(force){
  if (logCenter.busy || (logCenter.paused && !force) || document.hidden || window.__activeTab !== 'history') return;
  logCenter.busy = true;
  var generation = logCenter.generation;
  try {
    var r = await api('/api/logs?n=200&after=' + logCenter.cursor);
    if (generation !== logCenter.generation || window.__activeTab !== 'history') return;
    var box = $('recordsList');
    var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    var rows = (r.logs || []).filter(function(row){return !logCenter.ids.has(String(row.id));});
    if (rows.length) {
      if (!box.querySelector('.log-row')) box.innerHTML = '';
      rows.forEach(function(row){
        logCenter.ids.add(String(row.id));
        var level = /^(ERROR|CRITICAL|WARNING|INFO|DEBUG)$/.test(row.level) ? row.level : 'INFO';
        box.insertAdjacentHTML('beforeend', '<div class="log-row" data-log-id="' + esc(String(row.id)) + '">'
          + '<div class="log-meta"><time>' + esc(String(row.ts || '').replace('T',' ')) + '</time>'
          + '<span class="log-source">' + esc(row.source || '运行') + '</span>'
          + '<span class="log-level log-' + level.toLowerCase() + '">' + esc(level) + '</span></div>'
          + '<pre>' + esc(row.message || '') + '</pre></div>');
      });
      while (box.children.length > 500) {
        logCenter.ids.delete(box.firstElementChild.getAttribute('data-log-id'));
        box.firstElementChild.remove();
      }
      if (nearBottom) box.scrollTop = box.scrollHeight;
    } else if (!box.querySelector('.log-row')) box.innerHTML = '<div class="list-empty">等待新的运行日志…</div>';
    logCenter.cursor = Number(r.cursor) || logCenter.cursor;
    $('logsStatus').textContent = r.degraded ? '数据库暂不可用，显示当前进程暂存日志' : '实时更新 · 页面保留最近 500 条';
  } catch(e) {
    if (generation === logCenter.generation) $('logsStatus').textContent = '日志读取失败：' + e.message;
  } finally {
    logCenter.busy = false;
    scheduleLogPoll();
  }
}
async function loadRecords(){
  await pollLogCenter(true);
}
function downloadRuntimeLogs(){
  window.location.href = '/api/logs/download';
}
document.addEventListener('visibilitychange', function(){
  if (document.hidden) { clearTimeout(logCenter.timer); logCenter.timer = null; }
  else if (window.__activeTab === 'history' && !logCenter.paused) pollLogCenter();
});
function renderCleanupArchives(records){
  __recs = [];
  __recFull = {};
  $('archiveList').innerHTML = records.length ? records.map(recCard).join('') : '<div class="list-empty">暂无清理执行存档</div>';
}

function renderIngest(r){
  var st = r.stats || {}, tree = r.tree;
  if (!tree) {
    // 速报（非完整）模式：只有一段文本，去掉 Telegram 用的 Markdown 标记
    var plain = String(r.text || JSON.stringify(r, null, 2)).replace(/\*\*/g, '').replace(/`/g, '');
    return '<div class="ing-plain">' + esc(plain).replace(/\n/g, '<br>') + '</div>';
  }
  var srcs = ['本地影视库', '分享影视库', '其它库'];
  var h = '<div class="ing-sum">' +
    '<span class="ing-chip"><b>+' + (st.movies || 0) + '</b> 单片</span>' +
    '<span class="ing-chip"><b>+' + (st.series || 0) + '</b> 部连载</span>' +
    '<span class="ing-chip"><b>+' + (st.episodes || 0) + '</b> 集</span></div>';
  function group(src, cat, count, unit, body){
    return '<details class="ing-grp"' + (count <= 8 ? ' open' : '') + '><summary>' +
      '<span class="ing-src">' + esc(src.replace('影视库', '')) + '</span>' +
      '<span class="ing-cat">' + esc(cat) + '</span>' +
      '<span class="ing-cnt">' + count + unit + '</span></summary>' + body + '</details>';
  }
  var tv = tree.tv || {}, mov = tree.mov || {}, tvHtml = '', movHtml = '';
  var tvDetailAll = tree.tv_detail || {}, movDetailAll = tree.mov_detail || {};
  /* 「最近入库置顶」：组间按组内最新入库时间倒序，组内条目同样新的在前 */
  function ingDetailMap(kind, src, cat){
    var all = kind === 'tv' ? tvDetailAll : movDetailAll;
    return (all[src] && all[src][cat]) || {};
  }
  function ingGroupTs(kind, src, cat){
    var dmap = ingDetailMap(kind, src, cat), mx = 0;
    Object.keys(dmap).forEach(function(k){
      var v = dmap[k];
      var t = (kind === 'tv') ? (Number(v && v.last_ts) || 0) : (Number(v) || 0);
      if (t > mx) mx = t;
    });
    return mx;
  }
  function ingSortedCats(kind, src){
    return Object.keys((kind === 'tv' ? tv : mov)[src] || {}).sort(function(a, b){
      return ingGroupTs(kind, src, b) - ingGroupTs(kind, src, a);
    });
  }
  srcs.forEach(function(src){
    ingSortedCats('tv', src).forEach(function(cat){
      var detail = ingDetailMap('tv', src, cat);
      var shows = Object.keys(tv[src][cat]).map(function(n){ return [n, tv[src][cat][n]]; })
        .sort(function(a, b){
          var ta = Number((detail[a[0]] || {}).last_ts) || 0, tb = Number((detail[b[0]] || {}).last_ts) || 0;
          if (ta !== tb) return tb - ta;
          return b[1] - a[1];
        });
      tvHtml += group(src, cat, shows.length, '部',
        '<ul class="ing-list">' + shows.map(function(x){
          var info = detail[x[0]] || {};
          var seasons = info.seasons || {};
          var seasonParts = Object.keys(seasons).sort(function(a,b){
            if (a === 'unknown') return 1; if (b === 'unknown') return -1; return Number(a)-Number(b);
          }).map(function(sk){
            var sd = seasons[sk] || {}, eps = (sd.episodes || []).slice().sort(function(a,b){ return a-b; });
            var label = sk === 'unknown' ? '季未知' : ('S' + String(Number(sk)).padStart(2,'0'));
            if (eps.length) {
              var ranges = [], st = eps[0], pv = eps[0];
              for (var i=1; i<=eps.length; i++) {
                if (eps[i] === pv + 1) { pv = eps[i]; continue; }
                ranges.push(st === pv ? 'E' + String(st).padStart(2,'0') : 'E' + String(st).padStart(2,'0') + '–E' + String(pv).padStart(2,'0'));
                if (i < eps.length) st = pv = eps[i];
              }
              return label + ' ' + ranges.join('、');
            }
            return label + ' +' + (sd.count || 0) + '集';
          }).join(' · ');
          var suffix = seasonParts ? ' <span class="b">+' + x[1] + '集</span><span class="ing-season">' + esc(seasonParts) + '</span>' : ' <span class="b">+' + x[1] + '集</span>';
          return '<li><span class="n">《' + esc(x[0]) + '》</span>' + suffix + '</li>';
        }).join('') + '</ul>');
    });
    ingSortedCats('mov', src).forEach(function(cat){
      var dmap = ingDetailMap('mov', src, cat);
      var names = (mov[src][cat] || []).slice().sort(function(a, b){
        return (Number(dmap[b]) || 0) - (Number(dmap[a]) || 0);
      });
      movHtml += group(src, cat, names.length, '部',
        '<div class="ing-tags">' + names.map(function(n){ return '<span>《' + esc(n) + '》</span>'; }).join('') + '</div>');
    });
  });
  h += '<div class="ing-h">连载剧集 · ' + (st.series || 0) + ' 部</div>' + (tvHtml || '<div class="ing-empty">暂无新增剧集</div>');
  h += '<div class="ing-h">单片电影 · ' + (st.movies || 0) + ' 部</div>' + (movHtml || '<div class="ing-empty">暂无新增电影</div>');
  return h;
}
async function loadStats(full, force){
  var kw = (full ? 'full ' : '') + (force ? 'force' : '');
  var url = '/api/ingest?full=' + (full?1:0) + '&force=' + (force?1:0);
  $('statsOut').textContent = force ? '正在现场扫描 Emby（约 10-30 秒）...' : '读取缓存中...';
  $('statsCacheHint').textContent = '';
  try {
    var r = await api(url);
    $('statsOut').innerHTML = renderIngest(r);
    var age = r.cache_ts ? Math.round((Date.now()/1000) - r.cache_ts) : null;
    var ageStr = age == null ? '—' : (age < 60 ? age + ' 秒前' : (age < 3600 ? Math.floor(age/60) + ' 分钟前' : Math.floor(age/3600) + ' 小时前'));
    $('statsCacheHint').textContent = (r.from_cache ? '📦 来自缓存' : '🔄 现场扫描') + ' · ' + ageStr
    if (r.warning) $('statsCacheHint').textContent += ' · ⚠️ 扫描失败（' + r.warning + '），以上为旧数据';
  } catch(e){ $('statsOut').textContent = '错误: ' + e.message; }
}

