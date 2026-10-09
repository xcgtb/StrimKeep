/* ═══════════ 治理计划历史 ═══════════ */
var PLAN_TTL_MS = 2 * 3600 * 1000;
var PLAN_STATE = {
  pending:   { label: '待执行',   color: '#d97706', bg: 'rgba(217,119,6,.13)' },
  executing: { label: '执行中',   color: '#2563eb', bg: 'rgba(37,99,235,.13)' },
  done:      { label: '已完成',   color: '#16a34a', bg: 'rgba(22,163,74,.13)' },
  failed:    { label: '执行失败', color: '#dc2626', bg: 'rgba(220,38,38,.13)' },
  stale:     { label: '规则已变更', color: '#7c3aed', bg: 'rgba(124,58,237,.13)' },
  expired:   { label: '已过期',   color: '#6b7280', bg: 'rgba(107,114,128,.15)' }
};
var PLAN_KIND = { loc: '删本地', shr: '淘汰分享', keep: '受保护', exempt: '白名单豁免' };
function planStateOf(p){
  var st = p.state || 'pending';
  if (st === 'pending' && p.ts && Date.now() - p.ts * 1000 > PLAN_TTL_MS) st = 'expired';
  return st;
}
function planChip(text, color, bg){
  return '<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;color:' + color + ';background:' + bg + '">' + text + '</span>';
}
function planStateChip(st){
  var m = PLAN_STATE[st] || PLAN_STATE.expired;
  return planChip(esc(m.label), m.color, m.bg);
}
function planStatChips(s){
  s = s || {};
  var defs = [
    ['待删本地', s.loc, '#d97706', 'rgba(217,119,6,.13)'],
    ['淘汰分享', s.shr, '#dc2626', 'rgba(220,38,38,.12)'],
    ['受保护',   s.keep, '#16a34a', 'rgba(22,163,74,.12)'],
    ['白名单豁免', s.exempt, '#2563eb', 'rgba(37,99,235,.12)']
  ];
  return defs.map(function(d){
    var n = d[1] || 0;
    return n > 0 ? planChip(d[0] + ' ' + n, d[2], d[3]) : planChip(d[0] + ' 0', 'var(--text-dim)', 'transparent');
  }).join(' ');
}
function fmtPlanTime(ts){
  var d = new Date(ts * 1000), p2 = function(n){ return n < 10 ? '0' + n : '' + n; };
  return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
}
function fmtAgo(ts){
  var sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (sec < 60) return '刚刚';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
  return Math.floor(sec / 86400) + ' 天前';
}
async function loadPlans(){
  var limit = parseInt($('plansLimit').value) || 20;
  if (limit < 1) limit = 1;
  if (limit > 100) limit = 100;
  $('plansList').innerHTML = '<div class="list-empty">加载中...</div>';
  try {
    var r = await api('/api/plans?limit=' + limit);
    var list = r.plans || [];
    renderCleanupArchives(r.archives || []);
    if (!list.length) {
      $('plansList').innerHTML = '<div class="list-empty">暂无治理计划记录</div>';
      return;
    }
    $('plansList').innerHTML = list.map(function(p){
      var st = planStateOf(p), s = p.stats || {};
      var empty = !(s.loc || s.shr);
      var note = '';
      if (st === 'done' && p.executed_at) note = '执行于 ' + fmtPlanTime(p.executed_at);
      else if (st === 'pending' && empty) note = '本次扫描没有待清理项';
      else if (st === 'pending') note = '尚未执行，' + Math.max(0, Math.ceil((PLAN_TTL_MS - (Date.now() - p.ts * 1000)) / 60000)) + ' 分钟后过期';
      else if (st === 'expired') note = empty ? '无待清理项' : '超过 2 小时未执行，已作废';
      return '<div class="panel" style="padding:14px 16px;margin-bottom:12px">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px">'
        +   '<div><span style="font-weight:700;font-size:15px">' + (p.ts ? fmtPlanTime(p.ts) : '-') + '</span>'
        +   '<span style="font-size:12px;color:var(--text-dim);margin-left:8px">' + (p.ts ? fmtAgo(p.ts) : '') + '</span></div>'
        +   planStateChip(st)
        + '</div>'
        + '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">' + planStatChips(s) + '</div>'
        + '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'
        +   '<div style="font-size:12px;color:var(--text-mid)">' + esc(note)
        +   '<div style="font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--text-dim);margin-top:2px">plan_' + esc(p.id) + '</div></div>'
        +   '<button class="btn sm gray" onclick="showPlanDetail(&quot;' + esc(p.id) + '&quot;)">查看详情</button>'
        + '</div>'
        + '</div>';
    }).join('');
  } catch(e){
    $('plansList').innerHTML = '<div class="list-empty">加载失败: ' + esc(e.message) + '</div>';
  }
}

function showPlanAction(i){
  var p = window.__planView || {}, a = (p.actions || [])[i];
  if (!a) return;
  showGovDetail({ text: a.text, detail: a.detail || a.text, reason_label: a.reason_label, meta: a.meta || {},
                  files_count: (a.files || []).length }, a.kind,
                { executed: false, backPlan: p.id, ts: '' });
}
async function showPlanDetail(planId){
  try {
    var r = await api('/api/plan/' + planId);
    var p = r.plan || {};
    var actions = p.actions || [];
    var st = planStateOf(p);
    window.__planView = p;
    var html = '<h3>' + icon('bookmark') + '治理计划详情</h3>';
    html += '<div class="meta-row"><span class="k">状态</span><span class="v">' + planStateChip(st) + '</span></div>';
    html += '<div class="meta-row"><span class="k">生成时间</span><span class="v">' + (p.ts ? fmtPlanTime(p.ts) + '（' + fmtAgo(p.ts) + '）' : '-') + '</span></div>';
    if (p.executed_at) {
      html += '<div class="meta-row"><span class="k">执行时间</span><span class="v">' + fmtPlanTime(p.executed_at) + '</span></div>';
    }
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px;margin:10px 0">' + planStatChips(p.stats) + '</div>';
    // 按类型分组：先看会删本地的，再看淘汰分享的
    var groups = ['loc', 'shr', 'keep', 'exempt'], shown = 0, LIMIT = 100;
    html += '<div style="max-height:380px;overflow-y:auto;background:var(--surface-hover);border-radius:10px;padding:4px 10px">';
    groups.forEach(function(k){
      var items = actions.filter(function(a){ return a.kind === k; });
      if (!items.length) return;
      html += '<div style="font-size:12px;font-weight:700;margin:10px 0 4px;color:var(--text-mid)">' + PLAN_KIND[k] + '（' + items.length + '）</div>';
      items.forEach(function(a){
        if (shown >= LIMIT) return;
        shown++;
        html += '<div class="plan-row" style="padding:7px 0;border-bottom:1px solid var(--border);cursor:pointer" onclick="showPlanAction(' + actions.indexOf(a) + ')">';
        html += '<div style="font-size:13px;font-weight:600">' + esc(a.title || a.action_id || '') + (a.season != null ? '<span style="font-weight:400;color:var(--text-mid)"> · 第 ' + a.season + ' 季</span>' : '') + '</div>';
        html += '<div style="color:var(--text-mid);font-size:12px;margin-top:2px">' + esc(a.reason_label || a.reason || '') + '</div>';
        html += '</div>';
      });
    });
    if (!actions.length) html += '<div class="list-empty">这份计划没有待处理项</div>';
    if (actions.length > shown) html += '<div style="font-size:11px;color:var(--text-dim);padding:8px 0">仅显示前 ' + LIMIT + ' 条，共 ' + actions.length + ' 条</div>';
    html += '</div>';
    html += '<div style="margin-top:16px;text-align:right"><button class="btn gray" onclick="closeModal()">关闭</button></div>';
    openModal(html);
  } catch(e){
    toast('加载治理计划失败: ' + e.message, 'error');
  }
}

