/* TgtoDrive: user-triggered operations, separate from poster and Emby facts. */
var tgtoView = null, tgtoGeneration = 0;

function tgtoButton(media, label){
  var seed = {title:media.title || media.name || '', media_type:media.type || 'tv',
    tmdb_id:media.tmdb_id || '', year:media.year || ''};
  return '<button type="button" class="btn gray tgto-open" onclick="event.stopPropagation();tgtoOpen(' +
    esc(JSON.stringify(seed)) + ')">' + esc(label || '找资源') + '</button>';
}

async function loadTgtoConfig(){
  try {
    var r = await api('/api/upstream/config');
    var c = r.settings;
    $('tgtoEnabled').checked = c.enabled;
    $('tgtoUrl').value = c.url;
    $('tgtoToken').value = '';
    $('tgtoClearToken').checked = false;
    $('tgtoToken').placeholder = c.has_token ? '已保存令牌；留空保留' : '填写 OpenAPI 访问令牌';
    $('tgtoMaxPoints').value = c.max_points;
    $('tgtoConfigResult').textContent = c.enabled ? '上游接入已启用，找资源时按需连接' : '上游接入未启用';
  } catch(e){ $('tgtoConfigResult').textContent = '读取失败：' + e.message; }
}
function tgtoConfigBody(){
  return {enabled:$('tgtoEnabled').checked, url:$('tgtoUrl').value.trim(),
    token:$('tgtoToken').value.trim(), max_points:$('tgtoMaxPoints').value,
    clear_token:$('tgtoClearToken').checked};
}
async function tgtoSave(test){
  var btn = $(test ? 'tgtoTestBtn' : 'tgtoSaveBtn');
  btn.disabled = true;
  $('tgtoConfigResult').textContent = test ? '正在测试连接…' : '正在保存…';
  try {
    var r = await api('/api/upstream/' + (test ? 'test' : 'config'),
      {method:'POST', body:JSON.stringify(tgtoConfigBody()), timeoutMs:25000});
    if (test) {
      var c = r.capabilities;
      $('tgtoConfigResult').textContent = '连接成功 · 资源搜索' + (c.search ? '支持' : '未提供') +
        ' · 转存' + (c.transfer ? '支持' : '未提供') + ' · 整理记录' + (c.history ? '支持' : '未提供');
    } else {
      $('tgtoClearToken').checked = false;
      await loadTgtoConfig();
      toast('上游连接已保存', 'success');
    }
  } catch(e){ $('tgtoConfigResult').textContent = (test ? '连接测试失败：' : '保存失败：') + e.message; }
  finally { btn.disabled = false; }
}

function tgtoOpen(seed){
  tgtoGeneration++;
  tgtoView = {seed:seed, generation:tgtoGeneration, items:[], busy:false, historyPage:1, mode:'search'};
  var isTV = seed.media_type === 'tv';
  openModal('<section class="tgto-view" data-tgto-view="' + tgtoGeneration + '">' +
    '<h3>' + esc(seed.title) + ' · 上游资源</h3>' +
    '<p class="hint">搜索、转存由 TgtoDrive 执行；STRM 生成后，Emby 入库回调更新片库与订阅通知。</p>' +
    '<div class="tgto-filters">' +
    (isTV ? '<label>季（可选）<input id="tgtoSeason" type="number" min="0" max="999" placeholder="全部"></label>' +
      '<label>集（可选）<input id="tgtoEpisode" type="number" min="1" max="99999" placeholder="全部"></label>' : '') +
    '<label>目标网盘标识<input id="tgtoProvider" value="115" maxlength="50" placeholder="例如 115"></label></div>' +
    '<p class="hint">目标标识需与上游一致，转存目录沿用上游设置。补集时请核对本地／分享库缺失的季集，候选资源可能包含整季。</p>' +
    '<div class="tgto-actions"><button class="btn" id="tgtoSearchBtn" onclick="tgtoSearch()">搜索资源</button>' +
    '<button class="btn gray" onclick="tgtoHistory(1)">整理记录</button>' +
    '<button class="btn gray" onclick="tgtoJobs()">转存任务</button></div>' +
    '<p id="tgtoStatus" class="hint" role="status" aria-live="polite">点击搜索开始；未配置时请到规则设置 → 服务连接填写上游地址和令牌。</p>' +
    '<div id="tgtoResults"></div>' +
    '<button class="btn gray tgto-close" onclick="closeModal()">关闭</button></section>');
}
function tgtoActive(view){
  var section = $('modalBody').querySelector('[data-tgto-view]');
  return tgtoView === view && $('modalBg').classList.contains('show') && section &&
    Number(section.getAttribute('data-tgto-view')) === view.generation;
}
function tgtoResult(view, html, status){
  if (!tgtoActive(view)) return;
  $('tgtoResults').innerHTML = html;
  $('tgtoStatus').textContent = status || '';
}
async function tgtoSearch(){
  var view = tgtoView;
  if (!view || view.busy) return;
  view.busy = true; view.items = []; view.mode = 'search';
  var body = Object.assign({}, view.seed);
  if ($('tgtoSeason')) { body.season = $('tgtoSeason').value; body.episode = $('tgtoEpisode').value; }
  $('tgtoSearchBtn').disabled = true;
  tgtoResult(view, '', '正在查询上游资源…');
  try {
    var r = await api('/api/upstream/search', {method:'POST', body:JSON.stringify(body), timeoutMs:50000});
    view.items = r.items;
    tgtoResult(view, r.items.length ? r.items.map(function(item, index){
      return '<article class="tgto-resource"><strong>' + esc(item.title) + '</strong>' +
        '<p class="hint">' + esc([item.source,item.quality,item.size,item.points ? '积分 ' + item.points : ''].filter(Boolean).join(' · ')) + '</p>' +
        (item.transferable ? '<button class="btn" onclick="tgtoTransfer(' + index + ')">转存此资源</button>' :
          '<span class="hint">上游未返回可识别的转存标识，暂不允许转存</span>') + '</article>';
    }).join('') : '<p class="list-empty">上游未找到候选资源</p>',
    r.message + (r.truncated ? '；仅显示前 50 项，请按季集缩小范围' : ''));
  } catch(e){ tgtoResult(view, '', '资源搜索失败：' + e.message); }
  finally {
    view.busy = false;
    if (tgtoActive(view)) $('tgtoSearchBtn').disabled = false;
  }
}
async function tgtoTransfer(index){
  var view = tgtoView, item = view && view.items[index];
  if (!view || view.busy || !item || !item.handle) return;
  view.busy = true;
  var provider = $('tgtoProvider').value.trim();
  try {
    var cfg = await api('/api/upstream/config');
    if (!tgtoActive(view)) return;
    if (!confirm('转存《' + item.title + '》到 ' + provider + '？\n\n请核对作品、季集和版本。目录沿用上游设置；积分上限 ' + cfg.settings.max_points + '。\n已订阅作品将在 Emby 实际入库后通知。')) return;
    var operation = item.operation || (item.operation = tgtoOperationId());
    $('tgtoResults').querySelectorAll('button').forEach(function(b){ b.disabled = true; });
    $('tgtoStatus').textContent = '正在提交转存，请勿重复操作…';
    try {
      var r = await api('/api/upstream/transfer', {method:'POST', body:JSON.stringify({handle:item.handle,
        provider:provider, confirmed:true, operation_id:operation}), timeoutMs:30000});
      if (tgtoActive(view)) {
        $('tgtoStatus').textContent = r.job.message;
        view.mode = 'jobs';
        await tgtoJobs(view);
      }
    } catch(e){
      if (tgtoActive(view)) {
        $('tgtoStatus').textContent = '提交结果未确认：' + e.message + '。请查看转存任务及上游历史，不要直接重试。';
        // The same operation ID is retained if the user later retries manually.
      }
    }
  } catch(e){ if (tgtoActive(view)) $('tgtoStatus').textContent = '未提交：' + e.message; }
  finally { view.busy = false; }
}
function tgtoOperationId(){
  var values = new Uint32Array(4); window.crypto.getRandomValues(values);
  return Array.from(values).map(function(v){return v.toString(16).padStart(8,'0');}).join('');
}
async function tgtoJobs(pollView){
  var view = pollView || tgtoView;
  if (!view || !tgtoActive(view)) return;
  if (pollView && view.mode !== 'jobs') return;
  if (!pollView && view.busy) return;
  if (view.jobsLoading) return;
  view.mode = 'jobs';
  view.jobsLoading = true;
  try {
    var r = await api('/api/upstream/jobs', {timeoutMs:10000});
    var labels = {submitting:'正在提交',submitted:'上游已接收',unknown:'结果待核实'};
    if (!tgtoActive(view) || view.mode !== 'jobs') return;
    tgtoResult(view, r.items.length ? r.items.map(function(job){
      return '<article class="tgto-resource"><strong>' + esc(job.title) + '</strong>' +
        '<p>' + esc(labels[job.state] || '结果待核实') + ' · ' + esc(job.provider) + '</p>' +
        '<p class="hint">' + esc(job.message) + '</p>' +
        (job.state !== 'submitting' ? '<button class="btn gray" onclick="tgtoAcknowledge(' + jsarg(job.id) + ')">已核实，清除记录</button>' : '') + '</article>';
    }).join('') : '<p class="list-empty">暂无转存任务</p>',
    '这里显示转存提交结果；实际转存和 STRM 生成进度请查看上游，片库状态以 Emby 入库为准。');
    if (r.items.some(function(job){return job.state === 'submitting';}) && !view.jobPollPending) {
      view.jobPollPending = true;
      setTimeout(function(){ view.jobPollPending = false; if (tgtoActive(view)) tgtoJobs(view); }, 1500);
    }
  } catch(e){ if (tgtoActive(view) && view.mode === 'jobs') $('tgtoStatus').textContent = '任务查询失败：' + e.message + '；可手动重新查询'; }
  finally { view.jobsLoading = false; }
}
async function tgtoAcknowledge(id){
  if (!confirm('请先核实上游转存历史。\n清除本地记录不会取消上游任务，但会解除此资源的重复转存保护。继续？')) return;
  try {
    await api('/api/upstream/jobs/' + encodeURIComponent(id) + '/acknowledge', {method:'POST',body:'{"confirmed":true}'});
    await tgtoJobs();
  } catch(e){ toast(e.message,'error'); }
}
async function tgtoHistory(page){
  var view = tgtoView;
  if (!view || view.busy) return;
  view.historyPage = page; view.busy = true; view.mode = 'history';
  tgtoResult(view, '', '正在读取相关整理记录…');
  try {
    var r = await api('/api/upstream/history?keyword=' + encodeURIComponent(view.seed.title) + '&page=' + page, {timeoutMs:50000});
    tgtoResult(view, r.items.map(function(row){
      return '<article class="tgto-resource"><strong>' + esc(row.title || '整理记录 ' + row.id) + '</strong>' +
        '<p class="hint">' + esc([row.source,row.status].filter(Boolean).join(' · ')) + '</p>' +
        (/^\d+$/.test(row.id) ? '<button class="btn gray" onclick="tgtoDetail(' + jsarg(row.id) + ')">查看来源详情</button>' : '') + '</article>';
    }).join('') + '<div class="tgto-actions">' +
      (page > 1 ? '<button class="btn gray" onclick="tgtoHistory(' + (page-1) + ')">上一页</button>' : '') +
      (r.items.length === 20 ? '<button class="btn gray" onclick="tgtoHistory(' + (page+1) + ')">下一页</button>' : '') + '</div>',
    '按片名查询相关记录 · 第 ' + page + ' 页。记录尚未与本地 STRM 精确绑定，不作为删除配对依据。' + (r.items.length ? '' : ' 本页暂无记录。'));
  } catch(e){ tgtoResult(view, '', '整理记录查询失败：' + e.message); }
  finally { view.busy = false; }
}
async function tgtoDetail(id){
  var view = tgtoView;
  if (!view || view.busy) return;
  view.busy = true; view.mode = 'detail';
  try {
    var r = await api('/api/upstream/history/' + encodeURIComponent(id), {timeoutMs:50000});
    var labels = {title:'作品',name:'名称',year:'年份',tmdb_id:'TMDB',season:'季',episode:'集',source:'来源',status:'状态',
      original_name:'原文件名',file_name:'文件名',original_path:'原路径',target_path:'整理路径',strm_path:'STRM 路径',created_at:'时间',id:'编号',record_id:'记录编号'};
    tgtoResult(view, '<dl class="tgto-detail">' + Object.keys(r.fields).map(function(key){
      return '<dt>' + esc(labels[key] || key) + '</dt><dd>' + esc(r.fields[key]) + '</dd>';
    }).join('') + '</dl><button class="btn gray" onclick="tgtoHistory(' + view.historyPage + ')">返回记录</button>',
    '上游整理详情仅供核查；现有 STRM 与源文件删除规则保持不变。');
  } catch(e){ if(tgtoActive(view)) $('tgtoStatus').textContent = '详情读取失败：' + e.message; }
  finally { view.busy = false; }
}
