
/* ═══════════ 状态 ═══════════ */
var MENU = [
  { id: 'dashboard',  name: '媒体总览', icon: 'grid' },
  { id: 'governance', name: '双库治理', icon: 'scale' },
  { id: 'mapping',    name: '片库映射', icon: 'map' },
  { id: 'explore',    name: '影视探索', icon: 'film' },
  { id: 'subscribe',  name: '追更订阅', icon: 'bell' },
  { id: 'daily',      name: '入库与晨报', icon: 'file' },
  { id: 'history',    name: '日志中心', icon: 'list' },
  { id: 'settings',   name: '规则设置', icon: 'gear' }
];
/* 一体化分组：旧页签 id 全部保留为子页，switchTab('plans') 之类的老调用照常可用 */
var GROUPS = {
  governance: [['governance','清单'],['orphans','目录清理']],
  history:    [['history','实时日志'],['plans','计划存档']],
  daily:      [['daily','入库汇报'],['morning','晨报推送']]
};
function groupOf(id){
  for (var g in GROUPS) if (GROUPS[g].some(function(s){ return s[0] === id; })) return g;
  return id;
}
var SET_GROUPS = [['rules','治理规则','策略 · 特别篇 · 白名单'],['auto','自动任务','入库监控 · 定时巡检'],['conn','服务连接','Emby · TMDB · Telegram']];
var SET_MAP = ['rules','rules','rules','auto','auto','rules','conn','conn'];
function buildSubnavs(){
  Object.keys(GROUPS).forEach(function(g){
    GROUPS[g].forEach(function(cur){
      var sec = $('tab-' + cur[0]); if (!sec) return;
      sec.insertAdjacentHTML('afterbegin', '<div class="subnav">' + GROUPS[g].map(function(s){
        return '<button class="' + (s[0] === cur[0] ? 'on' : '') + '" onclick="switchTab(\'' + s[0] + '\')">' + s[1] + '</button>';
      }).join('') + '</div>');
    });
  });
  var st = $('tab-settings'); if (!st) return;
  var ps = Array.prototype.filter.call(st.children, function(c){ return c.classList.contains('panel'); });
  ps.forEach(function(p, i){ p.dataset.sg = SET_MAP[i] || 'rules'; });
  var opts = st.querySelectorAll('.strategy-option[data-v]');
  if (opts.length) { var w = document.createElement('div'); w.className = 'opt-grid'; opts[0].parentNode.insertBefore(w, opts[0]); opts.forEach(function(o){ w.appendChild(o); }); }
  var sv = st.querySelector('button[onclick^="saveConfig"]');
  if (sv && sv.parentNode) sv.parentNode.classList.add('save-bar');
  st.insertAdjacentHTML('afterbegin', '<div class="subnav rail" id="setNav">' + SET_GROUPS.map(function(s){
    return '<button data-g="' + s[0] + '" onclick="setSetGroup(\'' + s[0] + '\')"><b>' + s[1] + '</b><small>' + s[2] + '</small></button>';
  }).join('') + '</div>');
  var g0 = 'rules'; try { g0 = sessionStorage.getItem('setGroup') || 'rules'; } catch(e){}
  setSetGroup(g0);
}
function setSetGroup(g){
  try { sessionStorage.setItem('setGroup', g); } catch(e){}
  document.querySelectorAll('#setNav button').forEach(function(b){ b.classList.toggle('on', b.dataset.g === g); });
  document.querySelectorAll('#tab-settings > .panel').forEach(function(p){ p.style.display = p.dataset.sg === g ? '' : 'none'; });
}

var currentPlan = null;
var govData = { loc: [], shr: [], keep: [], exempt: [] };
var currentGovFilter = 'all';
/* 双库治理的扫描结果只存在内存里，浏览器一刷新（F5）就没了，用户经常因此
   "看不到数据"。这里把扫描结果顺带存一份到 sessionStorage，刷新后能原样恢复。 */
function saveGovStateToSession(){
  try {
    sessionStorage.setItem('govState', JSON.stringify({ currentPlan: currentPlan, govData: govData }));
  } catch(e){}
}
function restoreGovStateFromSession(){
  try {
    var raw = sessionStorage.getItem('govState');
    if (!raw) return false;
    var saved = JSON.parse(raw);
    if (!saved || !saved.govData) return false;
    currentPlan = saved.currentPlan || null;
    govData = saved.govData;
    if ($('govExecBtn')) $('govExecBtn').disabled = !currentPlan;
    $('govStats').style.display = '';
    $('gov-keep').textContent  = govData.keep.length;
    $('gov-loc').textContent   = govData.loc.length;
    $('gov-shr').textContent   = govData.shr.length;
    $('gov-exempt').textContent = (govData.exemptTotal != null ? govData.exemptTotal : govData.exempt.length);
    renderGovList();
    return true;
  } catch(e){ return false; }
}
/* ── 清单时效：Web 与 Bot/定时巡检共用同一份「最近扫描」，超过时效即作废，需重新扫描 ── */
var govPlanExpireAt = 0, govScanTs = 0, govPlanTtlMs = 2 * 3600 * 1000;
function fmtGovAgo(sec){
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + ' 秒前';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  return Math.floor(sec / 3600) + ' 小时' + Math.floor(sec % 3600 / 60) + ' 分钟前';
}
function setGovBanner(text, warn){
  var el = $('govBanner'); if (!el) return;
  if (!text){ el.style.display = 'none'; return; }
  el.style.display = '';
  el.style.color = warn ? '#f59e0b' : '#9ca3af';
  el.textContent = text;
}
function expireGovPlan(){
  currentPlan = null; govPlanExpireAt = 0;
  if ($('govExecBtn')) $('govExecBtn').disabled = true;
  clearGovStateSession();
  setGovBanner('⌛ 清单已过期，库内容可能已变化，请点「重新扫描」后再执行', true);
}
function tickGovBanner(){
  if (!currentPlan || !govPlanExpireAt) return;
  var leftMs = govPlanExpireAt - Date.now();
  if (leftMs <= 0){ expireGovPlan(); return; }
  setGovBanner('🕒 扫描于 ' + fmtGovAgo(Date.now() / 1000 - govScanTs) + ' · 清单约剩 ' +
               Math.ceil(leftMs / 60000) + ' 分钟有效，过期需重新扫描');
}
setInterval(tickGovBanner, 30000);
function fmtGovTs(ts){
  if (!ts) return '—';
  try { return new Date(ts * 1000).toLocaleString(); } catch(e){ return '—'; }
}
function renderGovTruth(d){
  var box=$('govTruth'), grid=$('govTruthGrid'), status=$('govTruthStatus');
  if (!box || !grid) return;
  box.style.display='';
  var c=d.consistency||{}, ing=c.ingest||{}, lib=c.library||{}, scan=d.scan||{}, r=scan.result||{}, strm=r.strm_counts||{};
  var rules=(c.rule_sig||'').slice(0,16)||'—';
  status.textContent=scan.ts ? '扫描于 '+fmtGovTs(scan.ts) : '尚无扫描结果';
  if ($('govReadyState')) $('govReadyState').textContent=scan.usable?'清单可执行':(scan.found?(scan.plan_state==='done'?'清理已执行':'需要重新扫描'):'等待扫描');
  if ($('govPendingCount')) $('govPendingCount').textContent='待处理 '+String(r.total_clean_cnt||0)+' 项 · '+String((r.del_local_files||0)+(r.del_share_files||0))+' 个 STRM';
  var rows=[
    ['扫描状态', scan.usable ? '可执行' : (scan.found ? (scan.plan_state==='done'?'已执行':'需重新扫描') : '尚无扫描')],
    ['身份冲突', String(r.identity_conflict_count||0)+' 个 TMDB 标识冲突（不自动删除）'],
    ['待处理', String(r.total_clean_cnt||0)+' 项 / '+String((r.del_local_files||0)+(r.del_share_files||0))+' 文件'],
    ['保护 / 豁免', String((r.protected_items||[]).length)+' / '+String(r.exempted_count||0)],
    ['静默跳过', String(r.quiet_skipped||0)+' 项'],
    ['STRM', '本地 '+String(strm.local||0)+' · 分享 '+String(strm.share||0)+' · 合计 '+String(strm.total||0)],
    ['片库快照', lib.ts ? fmtGovTs(lib.ts) : '暂无'],
    ['24H 入库快照', ing.ts ? fmtGovTs(ing.ts) : '暂无'],
    ['规则指纹', rules],
    ['扫描规则', (scan.rule_aligned === false ? '⚠️ 已变化，请重新扫描' : '✅ 与当前规则一致')]
  ];
  grid.innerHTML=rows.map(function(x){return '<div class="meta-row"><span class="k">'+esc(x[0])+'</span><span class="v">'+esc(x[1])+'</span></div>';}).join('');
  var conflicts=r.identity_conflicts||[], cb=$('govIdentityConflicts');
  if(cb){
    cb.style.display=conflicts.length?'':'none';
    if(conflicts.length){
      var kindLabel={movie:'电影',tv:'剧集'};
      cb.innerHTML='<div class="ic-h">⚠ TMDB 标识冲突 · '+conflicts.length+' 组 · 仅提示，不进入自动清理</div>'+conflicts.map(function(c){
        var kind=kindLabel[c.kind]||'TMDB';
        var rows=(c.identities||[]).map(function(x){
          return '<li><span class="lib '+(x.lib==='local'?'l':'s')+'">'+(x.lib==='local'?'本地':'分享')+'</span><span class="n">《'+esc(x.title||'—')+'》</span><span class="y">'+esc(x.year?'('+x.year+')':'')+'</span></li>';
        }).join('');
        return '<details class="ic-grp"><summary><span class="ic-kind">'+esc(kind)+'</span><span class="ic-id">TMDB '+esc(c.tmdb||'—')+'</span><span class="ic-cnt">'+esc(String(c.count||0))+' 个目录</span></summary><ul class="ic-list">'+rows+'</ul></details>';
      }).join('');
    } else {
      cb.innerHTML='';
    }
  }
}
async function loadGovTruth(){
  try { var d=await api('/api/governance/summary'); if(d.status==='success'){ renderGovTruth(d); return d; } } catch(e){}
  return null;
}

async function loadLatestGov(){
  try {
    var r = await api('/api/governance/latest');
    if (r.status !== 'success' || !r.found) return false;
    govPlanTtlMs = (r.ttl_sec || 7200) * 1000;
    govScanTs = Date.now() / 1000 - (r.age_sec || 0);
    if (!r.usable){
      currentPlan = null; govPlanExpireAt = 0;
      govData = { loc: [], shr: [], keep: [], exempt: [] };
      clearGovStateSession();
      if ($('govExecBtn')) $('govExecBtn').disabled = true;
      $('govStats').style.display = 'none';
      var why = { expired: '上次扫描已过期', used: '上次清单已执行完毕', running: '清理正在执行中' }[r.reason] || '清单不可用';
      $('planList').innerHTML = '<div class="list-empty">' + why + '，请点「重新扫描」获取最新状态</div>';
      setGovBanner('⌛ ' + why + '（扫描于 ' + fmtGovAgo(r.age_sec) + '），请重新扫描', true);
      return false;
    }
    var res = r.result || {};
    currentPlan = res.plan_id || null;
    govData = { loc: res.del_local_items || [], shr: res.del_share_items || [],
                keep: res.protected_items || [], exempt: res.exempted_items || [], exemptTotal: res.exempted_count };
    govPlanExpireAt = currentPlan ? Date.now() + (r.remaining_sec || 0) * 1000 : 0;
    if ($('govExecBtn')) $('govExecBtn').disabled = !currentPlan;
    $('govStats').style.display = '';
    $('gov-keep').textContent  = govData.keep.length;
    $('gov-loc').textContent   = govData.loc.length;
    $('gov-shr').textContent   = govData.shr.length;
    $('gov-exempt').textContent = (govData.exemptTotal != null ? govData.exemptTotal : govData.exempt.length);
    renderGovList();
    saveGovStateToSession();
    loadGovTruth();
    if (currentPlan) tickGovBanner();
    else setGovBanner('扫描于 ' + fmtGovAgo(r.age_sec) + ' · 无待清理项');
    return true;
  } catch(e){ return false; }
}
function clearGovStateSession(){
  try { sessionStorage.removeItem('govState'); } catch(e){}
}
var statsAutoLoaded = false;
var revealed = {};
var currentStrategy = { decision: 'quality_first', match_strategy: 'title_year', multi_season_protect: 'compare', tie_keep_local: false, season_replace_ratio: 0.9, exempt_keywords: [], special_action: 'compare' };
var currentSubs = [];
var currentGovItems = [];

/* 缓存 Emby 主机地址（由 /api/dashboard 或 /api/emby/library 填充） */
window.__embyHost = '';

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
// 内联事件属性里传字符串参数：JSON 字符串 + HTML 转义，避免 esc() 后 &#39; 被解码回 ' 造成 JS 字符串逃逸
function jsarg(s){ return esc(JSON.stringify(String(s == null ? '' : s))); }

function toast(msg, type, ms){
  if (typeof type === 'number'){ ms = type; type = ''; }
  ms = ms || 2600;
  var t = $('toast');
  var prefix = type === 'success' ? icon('check') : (type === 'error' ? icon('x') : '');
  t.className = 'toast ' + (type || '');
  t.innerHTML = prefix;
  var sp = document.createElement('span'); sp.textContent = String(msg == null ? '' : msg); t.appendChild(sp);
  t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(function(){ t.classList.remove('show'); }, ms);
}
var __taskHintControl = null;
var __scanBusy = false;
function showTaskHint(text){
  $('taskHintText').textContent = text || '任务中...';
  var close = $('taskHintClose'), scan = !!__taskHintControl;
  close.disabled = scan && __taskHintControl.requested;
  close.title = scan ? '取消本次双库扫描' : '收起提示（清理仍在后台运行）';
  close.setAttribute('aria-label', scan ? '取消本次双库扫描' : '收起任务提示');
  $('taskHint').classList.add('show');
  document.body.classList.add('task-running');
}
function updateTaskHint(text){ $('taskHintText').textContent = text; }   // 只改文字，不会把用户手动收起的提示条又弹出来
function hideTaskHint(){
  $('taskHint').classList.remove('show');
  document.body.classList.remove('task-running');
}
function onTaskHintClose(){
  if (__taskHintControl) return requestScanCancel(__taskHintControl);
  hideTaskHint();
}
async function requestScanCancel(control){
  if (control.sending || control.accepted) return;
  control.requested = true;
  $('taskHintClose').disabled = true;
  updateTaskHint('正在取消双库扫描…');
  // 扫描刚发起、尚未拿到任务编号时，先记住取消，拿到编号后立即发送。
  if (!control.taskId) return;
  control.sending = true;
  try {
    var r = await api('/api/task/' + control.taskId + '/cancel', {method:'POST', timeoutMs:10000});
    control.accepted = !!r.cancel_requested;
    if (!control.accepted) control.requested = false; // 已完成，以任务最终结果为准。
  } catch(e){
    control.requested = false;
    if (__taskHintControl === control) toast('取消未确认：' + e.message, 'error', 4000);
  } finally {
    control.sending = false;
    if (__taskHintControl === control) {
      $('taskHintClose').disabled = control.requested;
      if (!control.requested) updateTaskHint('扫描双库中…');
    }
    if (control.wake) control.wake();
  }
}
var __hintPolls = 0;   // 正在轮询的任务数：只有最后一个结束才收起提示条，避免互相误关/漏关
function fmtElapsed(sec){ var m = Math.floor(sec / 60), s = sec % 60; return m + ':' + (s < 10 ? '0' : '') + s; }
function toggleSidebar(open){ var sb = $('sidebar'); if(open){ sb.classList.add('open'); $('mask').classList.add('show'); document.body.classList.add('drawer-open'); } else { sb.classList.remove('open'); $('mask').classList.remove('show'); document.body.classList.remove('drawer-open'); } }
function openModal(html){ $('modalBody').innerHTML = html; $('modalBg').classList.add('show'); }
function closeModal(){ $('modalBg').classList.remove('show'); }

function getHeaders(){
  return { 'Content-Type': 'application/json' };
}
var __loginShown = false;
function showLoginOverlay(){
  if (__loginShown) return;
  __loginShown = true;
  var d = document.createElement('div');
  d.id = 'loginOverlay';
  d.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(10,14,26,.92);display:flex;align-items:center;justify-content:center;padding:20px';
  d.innerHTML = '<div style="background:var(--surface,#fff);color:var(--text,#111);border-radius:16px;padding:24px;width:100%;max-width:340px;box-shadow:0 10px 40px rgba(0,0,0,.4)">'
    + '<div style="font-size:18px;font-weight:700;margin-bottom:16px">登录 StrimKeep</div>'
    + '<input id="loginUser" type="text" name="username" autocomplete="username" placeholder="用户名" value="admin" style="width:100%;margin-bottom:10px;font-size:16px;padding:10px">'
    + '<input id="loginPass" type="password" name="password" autocomplete="current-password" placeholder="密码" style="width:100%;margin-bottom:10px;font-size:16px;padding:10px">'
    + '<div id="loginErr" style="color:#ef4444;font-size:13px;min-height:18px;margin-bottom:6px"></div>'
    + '<button class="btn" id="loginBtn" style="width:100%;padding:11px;font-size:15px">登录</button>'
    + '</div>';
  document.body.appendChild(d);
  var go = async function(){
    $('loginErr').textContent = '';
    try {
      var r = await fetch('/api/login', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ username: $('loginUser').value, password: $('loginPass').value }) });
      if (!r.ok) { $('loginErr').textContent = '用户名或密码错误'; return; }
      location.reload();
    } catch(e){ $('loginErr').textContent = '登录失败：' + e.message; }
  };
  $('loginBtn').onclick = go;
  $('loginPass').addEventListener('keydown', function(e){ if (e.key === 'Enter') go(); });
  setTimeout(function(){ $('loginPass').focus(); }, 50);
}
async function api(path, opts){
  opts = Object.assign({}, opts || {});
  var timeoutMs = opts.timeoutMs; delete opts.timeoutMs;
  var headers = Object.assign(getHeaders(), opts.headers || {});
  var ctl = null, timer = null;
  var parentSignal = opts.signal, parentAbort = null;
  if (timeoutMs) {
    ctl = new AbortController(); opts.signal = ctl.signal;
    if (parentSignal) {
      parentAbort = function(){ctl.abort();};
      if (parentSignal.aborted) ctl.abort();
      else parentSignal.addEventListener('abort', parentAbort, {once:true});
    }
    timer = setTimeout(function(){ ctl.abort(); }, timeoutMs);
  }
  try {
    var r = await fetch(path, Object.assign({}, opts, { headers: headers }));
    if (r.status === 401) {
      // 未登录或会话过期：弹出自带登录页（登录一次后 30 天免输）
      showLoginOverlay();
      throw new Error('未登录或登录已过期');
    }
    var j = await r.json().catch(function(){ return { detail: '响应解析失败' }; });
    if (!r.ok) throw new Error(j.detail || j.message || ('HTTP ' + r.status));
    return j;
  } catch(e){
    if (e && e.name === 'AbortError') throw new Error('请求超时');
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
    if (parentSignal && parentAbort) parentSignal.removeEventListener('abort', parentAbort);
  }
}
async function pollTask(taskId, hintText, control){
  hintText = hintText || '任务中...';
  var t0 = Date.now(), POLL_MAX_MS = 20 * 60 * 1000, fails = 0, wake = null;
  function sleep(ms){ return new Promise(function(r){ wake = r; if(control) control.wake = r; setTimeout(r, ms); }); }
  // 手机切到后台再回来 / 网络恢复时，立刻补查一次，不用干等下一个 1.2 秒
  var onWake = function(){ if (!document.hidden && wake) wake(); };
  document.addEventListener('visibilitychange', onWake);
  window.addEventListener('online', onWake);
  __hintPolls++;
  __taskHintControl = control || null;
  showTaskHint(control && control.requested ? '正在取消双库扫描…' : hintText);
  try {
    for(;;){
      var t;
      try {
        // 每次查询最多等 10 秒：网络切换后旧连接会一直挂起，没有超时就永远卡在这里
        t = await api('/api/task/' + taskId, { timeoutMs: 10000 });
        fails = 0;
      } catch(e){
        var m = (e && e.message) || '';
        if (/任务不存在|已过期/.test(m)) throw new Error('任务已丢失（服务可能刚重启），请重新扫描');
        if (/未登录|登录已过期/.test(m)) throw e;
        // 网络抖动 / 502（服务重启中）/ 请求超时：容忍一阵子再放弃
        if (++fails >= 10) throw new Error('与服务的连接中断，请检查网络后重试');
        await sleep(1500);
        continue;
      }
      if (t.status !== 'running') return t;
      var el = Math.floor((Date.now() - t0) / 1000);
      if (el * 1000 > POLL_MAX_MS) throw new Error('等待超过 20 分钟，已停止等待（任务可能仍在后台运行，稍后再查看结果）');
      updateTaskHint((control && (control.requested || t.cancel_requested) ? '正在取消双库扫描…' : hintText) + '  ' + fmtElapsed(el));
      await sleep(1200);
    }
  } finally {
    document.removeEventListener('visibilitychange', onWake);
    window.removeEventListener('online', onWake);
    __hintPolls = Math.max(0, __hintPolls - 1);
    if (__taskHintControl === control) __taskHintControl = null;
    if (control) control.wake = null;
    if (__hintPolls === 0) hideTaskHint();   // 无论成功/失败/异常都必须收起
  }
}
// 从 bfcache / 后台恢复页面时，如果已经没有在跑的轮询，确保提示条不会残留
window.addEventListener('pageshow', function(){ if (__hintPolls === 0) hideTaskHint(); });
function statusOk(txt){ return '<span class="status-badge ok">' + icon('check') + txt + '</span>'; }
function statusNo(txt){ return '<span class="status-badge no">' + icon('x') + txt + '</span>'; }
function statusDim(txt){ return '<span class="status-badge dim">' + txt + '</span>'; }

/* ═══════════ 主题切换 ═══════════ */
var THEME_KEY = 'strimkeep-theme';

function getCurrentTheme(){
  var saved = '';
  try { saved = localStorage.getItem(THEME_KEY) || ''; } catch(e){}
  if (saved === 'dark' || saved === 'light') return saved;
  // 未设置：跟随系统
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function updateThemeIcon(){
  var btn = $('themeBtn');
  if (!btn) return;
  var isDark = getCurrentTheme() === 'dark';
  // 月亮图标：当前是深色时，按钮显示太阳（点了变亮）
  btn.innerHTML = isDark
    ? '<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>'
    : '<svg class="ic" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  btn.title = isDark ? '切换到浅色模式' : '切换到深色模式';
  updateSideTheme();
}

function updateSideTheme(){
  var el = $('sideThemeIcon'), txt = $('sideThemeText');
  if (!el || !txt) return;
  var isDark = getCurrentTheme() === 'dark';
  el.innerHTML = isDark
    ? '<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>'
    : '<svg class="ic" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  txt.textContent = isDark ? '浅色模式' : '深色模式';
}


function toggleTheme(){
  var cur = getCurrentTheme();
  var next = cur === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, next); } catch(e){}
  document.documentElement.setAttribute('data-theme', next);
  updateThemeIcon();
  toast(next === 'dark' ? '已切换到深色模式' : '已切换到浅色模式', 'success', 1500);
}

function initTheme(){
  // <head> 里的内联脚本已经设了 data-theme（防闪烁），这里只更新图标
  updateThemeIcon();
  // 监听系统主题变化（仅在用户未手动选择时生效）
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(){
      var saved = '';
      try { saved = localStorage.getItem(THEME_KEY) || ''; } catch(e){}
      if (!saved) {
        document.documentElement.removeAttribute('data-theme');
        updateThemeIcon();
      }
    });
  }
}

/* ═══════════ 带鉴权的海报加载 ═══════════ */
/* <img src> 不会自动带 Basic Auth 头，必须走 fetch 拿 blob 再赋值。
   HTML 里统一用 data-emby-src 标记需要鉴权加载的图片。 */
/* 海报加载：并发 + CacheStorage。Emby 图片需要鉴权，不能直接 <img src>，
   但可以把带鉴权拿到的响应缓存到浏览器，后续切页/刷新直接命中。 */
var POSTER_MAX_CONCURRENCY = 6;
var POSTER_CACHE_NAME = 'strimkeep-posters-v1';
var __posterInflight = {};
var __posterCacheWrites = 0;
async function posterFetch(url){
  if (!url) throw new Error('empty poster');
  if (__posterInflight[url]) return __posterInflight[url];
  __posterInflight[url] = (async function(){
    var cache = null;
    try { if (window.caches) cache = await caches.open(POSTER_CACHE_NAME); } catch(e) {}
    if (cache) {
      try {
        var hit = await cache.match(url);
        if (hit) return await hit.blob();
      } catch(e) {}
    }
    var controller = new AbortController(), timer = setTimeout(function(){controller.abort();}, 10000);
    try {
      var r = await fetch(url, {credentials:'same-origin', cache:'force-cache', signal:controller.signal});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var saved = r.clone(), blob = await r.blob();
      if (cache) {
        try {
          await cache.put(url, saved);
          if (typeof __posterCacheWrites === 'number' && ++__posterCacheWrites % 16 === 0) {
            var keys = await cache.keys();
            await Promise.all(keys.slice(0, Math.max(0, keys.length - 120)).map(function(key){return cache.delete(key);}));
          }
        } catch(e) {}
      }
      return blob;
    } finally {clearTimeout(timer);}
  })().finally(function(){ delete __posterInflight[url]; });
  return __posterInflight[url];
}
function clearPosterLoading(img){
  var loading = img.parentNode && img.parentNode.querySelector('.poster-loading');
  if (loading) loading.remove();
}
function posterImageError(img){
  clearPosterLoading(img);
  var placeholder = document.createElement('div');
  placeholder.className = 'no-img';
  placeholder.textContent = '暂无海报';
  if (img.parentNode) img.parentNode.replaceChild(placeholder, img);
}
var __posterQueue = [], __posterActive = 0, __posterObserver = null, __posterObserved = new Set();
function enqueuePoster(img){
  if (img.getAttribute('data-loaded')) return Promise.resolve();
  img.setAttribute('data-loaded', '1');
  return new Promise(function(resolve){
    __posterQueue.push({img:img, resolve:resolve});
    drainPosterQueue();
  });
}
function drainPosterQueue(){
  while (__posterActive < POSTER_MAX_CONCURRENCY && __posterQueue.length) {
    var job = __posterQueue.shift();
    if (job.img.isConnected === false) {job.img.removeAttribute('data-loaded');job.resolve();continue;}
    __posterActive += 1;
    loadPosterImage(job.img).finally(function(){__posterActive -= 1;drainPosterQueue();}).then(job.resolve, job.resolve);
  }
}
async function loadPosterImage(img){
  try {
    var blob = await posterFetch(img.getAttribute('data-emby-src'));
    if (img.isConnected === false) {img.removeAttribute('data-loaded');return;}
    var objUrl = URL.createObjectURL(blob), released = false;
    function release(){
      if (released) return;
      released = true;
      URL.revokeObjectURL(objUrl);
      img.removeEventListener('load', release);
      img.removeEventListener('error', failed);
    }
    function failed(){if (!released) {release();posterImageError(img);}}
    // 在赋值前绑定，命中缓存立即完成及加载失败也能释放对象 URL。
    img.addEventListener('load', release, {once:true});
    img.addEventListener('error', failed, {once:true});
    img.src = objUrl;
    if (img.decode) img.decode().then(function(){clearPosterLoading(img);release();}, failed);
    if (img.complete && img.naturalWidth) {clearPosterLoading(img);release();}
  } catch(e) {posterImageError(img);}
}
async function hydratePosters(root){
  var scope = root || document, tasks = [];
  if (__posterObserver) __posterObserved.forEach(function(img){
    if (img.isConnected === false) {__posterObserver.unobserve(img);__posterObserved.delete(img);}
  });
  // 原生海报走浏览器懒加载；当前屏幕内的图片优先，其余不与可见图抢带宽。
  scope.querySelectorAll('.poster-wrap img').forEach(function(img){
    var rect = img.getBoundingClientRect();
    if (rect.width > 0 && rect.bottom >= 0 && rect.top < window.innerHeight + 150) {
      img.loading = 'eager';
      img.fetchPriority = 'high';
    }
  });
  if (!__posterObserver && window.IntersectionObserver) {
    __posterObserver = new IntersectionObserver(function(entries){
      entries.forEach(function(entry){
        if (!entry.isIntersecting) return;
        __posterObserver.unobserve(entry.target);
        __posterObserved.delete(entry.target);
        enqueuePoster(entry.target);
      });
    }, {rootMargin:'600px 0px', threshold:0});
  }
  scope.querySelectorAll('img[data-emby-src]:not([data-loaded])').forEach(function(img){
    if (!__posterObserver) {tasks.push(enqueuePoster(img));return;}
    var rect = img.getBoundingClientRect();
    if (rect.width > 0 && rect.bottom >= -600 && rect.top <= window.innerHeight + 600) tasks.push(enqueuePoster(img));
    else {__posterObserved.add(img);__posterObserver.observe(img);}
  });
  await Promise.all(tasks);
}

function renderMenu(){
  var nav = $('menu');
  var current = window.__activeTab || 'dashboard';
  var groups = [
    {title:'媒体库',items:[['dashboard','媒体总览','grid','main'],['mapping','片库映射','map','main'],['explore','影视探索','film','main']]},
    {title:'治理与追更',items:[['governance','双库治理','scale','main'],['orphans','目录清理','trash','main'],['subscribe','追更订阅','bell','main'],['daily','入库与晨报','file','main']]},
    {title:'运行与设置',items:[['history','日志中心','activity','main'],['plans','计划存档','archive','main'],['settings','规则设置','gear','main']]}
  ];
  nav.innerHTML = groups.map(function(g){
    var links = g.items.map(function(item){
      var id=item[0], label=item[1], ico=item[2], kind=item[3];
      var target = id;
      var active = current === id || (id === 'daily' && current === 'morning');
      return '<a class="'+(kind==='sub'?'sub ':'')+(active?'active':'')+'" data-tab="'+target+'" onclick="switchTab(\''+target+'\')">' +
             '<span class="icon">'+icon(ico)+'</span><span class="nav-label">'+label+'</span></a>';
    }).join('');
    return '<div class="nav-group"><div class="nav-group-title"><span>'+g.title+'</span></div>'+links+'</div>';
  }).join('');
  updateSideTheme();
}

function switchTab(id){
  if (window.__activeTab === 'settings' && id !== 'settings' && typeof coverIsDirty === 'function' && coverIsDirty()) {
    if (!confirm('「画质对比规则」有未保存的改动，离开将丢弃。\n\n确定离开？')) return;
  }
  if (window.__activeTab === 'explore' && id !== 'explore') pauseExplore();
  window.__activeTab = id;
  if (id !== 'history' && typeof stopLogCenter === 'function') stopLogCenter();
  document.querySelectorAll('.sidebar nav a').forEach(function(a){
    var target = a.dataset.tab;
    var isSub = a.classList.contains('sub');
    var active = target === id || (target === 'daily' && id === 'morning');
    a.classList.toggle('active', active);
  });
  document.querySelectorAll('.mobile-dock button[data-dock-tab]').forEach(function(b){ b.classList.toggle('active', b.dataset.dockTab === groupOf(id)); });
  document.querySelectorAll('.tab').forEach(function(s){ s.classList.remove('active'); });
  var tab = $('tab-' + id); if(tab) tab.classList.add('active');
  var m = MENU.find(function(x){ return x.id === groupOf(id); });
  var pageTitles = {orphans:'目录清理', plans:'计划存档', morning:'晨报推送'};
  $('tabTitle').textContent = pageTitles[id] || (m ? m.name : 'StrimKeep');
  window.scrollTo(0, 0);
  toggleSidebar(false);
  // 记住当前所在页签，浏览器刷新（F5）后停在原地，而不是每次都跳回治理总览
  // 重新拉取一遍数据，打断正在查看的内容。
  try { sessionStorage.setItem('lastTab', id); } catch(e){}

  if (id === 'dashboard')  loadDashboard();
  if (id === 'governance') { if (!currentPlan) restoreGovStateFromSession(); loadLatestGov(); loadGovTruth(); }
  if (id === 'explore')    ensureExploreLoaded();
  if (id === 'settings')   { loadConfig(); loadBotStatus(); loadStrategy(); loadIngest(); loadGovAuto(); loadCoverStrategy(); }
  if (id === 'mapping')    ensureEmbyLoaded();
  if (id === 'subscribe')  loadSubscriptions();
  if (id === 'morning')    loadMorning();
  if (id === 'history')    loadRecords();
  if (id === 'plans')      loadPlans();
  if (id === 'daily' && !statsAutoLoaded) { statsAutoLoaded = true; loadStats(false, false); }
}

/* 手机 Floating Dock：向下滚动时收起，停止/向上滚动或接近底部时恢复。页面本身始终保留安全底距。 */
(function(){
  var lastY = 0, ticking = false;
  function updateDock(){
    var dock = $('mobileDock'); if (!dock || window.innerWidth > 899) return;
    var y = window.scrollY || document.documentElement.scrollTop || 0;
    var max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    var nearBottom = max - y < 48;
    if (nearBottom || y < 8 || y < lastY) dock.classList.remove('dock-hidden');
    else if (y > lastY + 8) dock.classList.add('dock-hidden');
    lastY = y;
    ticking = false;
  }
  window.addEventListener('scroll', function(){
    if (!ticking){ requestAnimationFrame(updateDock); ticking = true; }
  }, {passive:true});
  window.addEventListener('resize', function(){ if (window.innerWidth > 899 && $('mobileDock')) $('mobileDock').classList.remove('dock-hidden'); });
})();

