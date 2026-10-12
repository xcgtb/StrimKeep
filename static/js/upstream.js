/* TgtoDrive: user-triggered operations, separate from poster and Emby facts. */
var tgtoView = null, tgtoGeneration = 0;

function tgtoPosterAttrs(media){
  var seed = {title:media.title || media.name || '', media_type:media.type || 'tv',
    tmdb_id:media.tmdb_id || '', year:media.year || '', poster:media.poster || '',
    rating:media.rating || '', library:media.library || '', overview:media.overview || ''};
  var arg = jsarg(JSON.stringify(seed));
  return ' role="button" tabindex="0" aria-label="查看 ' + esc(seed.title) + ' 的资源详情"' +
    ' onclick="tgtoOpen(JSON.parse(' + arg + '))" onkeydown="tgtoPosterKey(event,' + arg + ')"';
}
function tgtoPosterKey(event, seed){
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault(); tgtoOpen(JSON.parse(seed));
  }
}

function tgtoButton(media, label){
  var seed = {title:media.title || media.name || '', media_type:media.type || 'tv',
    tmdb_id:media.tmdb_id || '', year:media.year || '', poster:media.poster || '',
    rating:media.rating || '', library:media.library || '', overview:media.overview || ''};
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
  tgtoView = {seed:seed, generation:tgtoGeneration, items:[], busy:false, historyPage:1, mode:'search', filters:{}, searched:false};
  var view = tgtoView;
  var isTV = seed.media_type === 'tv';
  openModal('<section class="tgto-view" data-tgto-view="' + tgtoGeneration + '">' +
    '<div class="tgto-top"><span>作品详情</span><button type="button" class="tgto-dismiss" onclick="closeModal()" aria-label="关闭资源详情">×</button></div>' +
    '<header class="tgto-hero"><div class="tgto-poster">' + tgtoPoster(seed.poster) + '</div><div class="tgto-hero-info">' +
    '<span class="tgto-eyebrow">' + (isTV ? '电视剧' : '电影') + '</span><h3>' + esc(seed.title) + '</h3>' +
    '<div class="tgto-meta">' + (seed.rating ? '<span class="tgto-rating">★ ' + esc(seed.rating) + '</span>' : '') +
    (seed.year ? '<span>' + esc(seed.year) + '</span>' : '') + (seed.tmdb_id ? '<span>TMDB ' + esc(seed.tmdb_id) + '</span>' : '') + '</div>' +
    (seed.library ? '<span class="tgto-library">' + esc(seed.library) + '</span>' : '') +
    (seed.overview ? '<p class="tgto-overview">' + esc(seed.overview) + '</p>' : '') +
    '<p class="tgto-flow">找资源 → 转存 → 整理入库</p><button class="btn gray" onclick="tgtoSubscribeResource()">订阅资源获取</button></div></header>' +
    '<nav class="tgto-tabs" aria-label="资源详情视图"><button class="active" data-tgto-tab="search" onclick="tgtoResources()">资源发现</button>' +
    '<button data-tgto-tab="history" onclick="tgtoHistory(1)">整理记录</button>' +
    '<button data-tgto-tab="jobs" onclick="tgtoJobs()">上游任务</button>' +
    '<button data-tgto-tab="subscriptions" onclick="tgtoResourceSubscriptions()">资源订阅</button></nav>' +
    '<div class="tgto-search-tools" id="tgtoSearchTools"><div class="tgto-search-line">' +
    (isTV ? '<label>季<input id="tgtoSeason" type="number" min="0" max="999" placeholder="全部"></label>' +
      '<label>集<input id="tgtoEpisode" type="number" min="1" max="99999" placeholder="全部"></label>' : '') +
    '<button class="btn" id="tgtoSearchBtn" onclick="tgtoSearch()">重新搜索</button>' +
    '<label class="tgto-target">转存到<select id="tgtoTarget" onchange="tgtoChooseTarget()"><option value="">读取转存目标…</option></select></label></div>' +
    '<details class="tgto-advanced"><summary>高级设置</summary><label>目标标识<input id="tgtoProvider" value="115" maxlength="50" onchange="tgtoDirectoryText()" placeholder="与上游目标标识一致"></label>' +
    '<p id="tgtoProviderHint" class="hint">手填的是网盘目标标识，保存目录沿用上游设置。</p></details><p id="tgtoDirectoryInfo" class="hint">正在读取上游保存目录…</p>' +
    '<details id="tgtoFilterPanel" class="tgto-filter-panel" open><summary>筛选资源</summary><div id="tgtoFacets"></div></details></div>' +
    '<p id="tgtoStatus" class="tgto-status" role="status" aria-live="polite">正在准备资源搜索…</p>' +
    '<div id="tgtoResults"></div><footer class="tgto-footer"><span>入库状态以 Emby 为准；转存前核对版本和季集。</span>' +
    '<button class="btn gray" onclick="closeModal()">关闭</button></footer></section>');
  $('tgtoResults').innerHTML = '';
  if (typeof hydratePosters === 'function') hydratePosters($('modalBody'));
  setTimeout(async function(){
    if (!tgtoActive(view) || view.searched || view.busy) return;
    await tgtoSearch();
    if (tgtoActive(view)) {tgtoLoadTargets(view);tgtoLoadDirectories(view);}
  }, 0);
}
function tgtoPoster(url){
  if (typeof url === 'string' && /^https:\/\/image\.tmdb\.org\/t\/p\/[^/]+\/[^/?#]+$/.test(url)) url = '/api/tmdb/poster/' + url.split('/').pop();
  var decoded = typeof posterDecodedMarkup === 'function' && typeof url === 'string' ? posterDecodedMarkup(url) : '';
  if (decoded) return decoded;
  return typeof url === 'string' && /^\/api\/(?:tmdb|emby)\/poster\//.test(url) && !/[<>"'\\\s?#]/.test(url)
    ? '<img data-emby-src="' + esc(url) + '" alt="作品海报" decoding="async">' : '<span>暂无海报</span>';
}
async function tgtoLoadTargets(view){
  var initial = $('tgtoProvider').value;
  try {
    var r = await api('/api/upstream/providers', {timeoutMs:50000});
    if (!tgtoActive(view)) return;
    view.targets = r.items;
    $('tgtoTarget').innerHTML = '<option value="">手填目标标识</option>' + r.items.map(function(x){return '<option value="' + esc(x.id) + '">' + esc(x.name) + '</option>';}).join('');
    var match = r.items.find(function(x){return x.id === $('tgtoProvider').value;});
    if (match) $('tgtoTarget').value = match.id;
    else if (r.items.length && $('tgtoProvider').value === initial) { $('tgtoTarget').value = r.items[0].id; tgtoChooseTarget(); }
    $('tgtoProviderHint').textContent = '目标来自上游配置，转存目录沿用上游设置。';
  } catch(e){
    if (tgtoActive(view)) {
      $('tgtoTarget').innerHTML = '<option value="">手填目标标识</option>';
      $('tgtoProviderHint').textContent = e.message + '；请核对高级设置中的目标标识。';
    }
  }
}
function tgtoChooseTarget(){ if ($('tgtoTarget').value) $('tgtoProvider').value = $('tgtoTarget').value; tgtoDirectoryText(); }
async function tgtoLoadDirectories(view){
  try {var r = await api('/api/upstream/directories');if (tgtoActive(view)) {view.directories=r.items;tgtoDirectoryText();}}
  catch(e){if(tgtoActive(view)) $('tgtoDirectoryInfo').textContent='保存目录未能读取：'+e.message+'。请到上游核对；此处不能自定义目录。';}
}
function tgtoDirectoryText(){
  var view=tgtoView;if(!view || !tgtoActive(view))return;
  var row=(view.directories||view.targets||[]).find(function(x){return x.id===$('tgtoProvider').value;});
  $('tgtoDirectoryInfo').textContent=row && (row.directory_id||row.directory_path)
    ? '上游保存目录：'+(row.directory_path||'路径未提供')+' · ID '+(row.directory_id||'未提供')+'（目录修改请到上游）'
    : '上游未提供此目标的保存目录 ID / 路径，请在上游配置并核对。';
}
function tgtoTabs(view){
  if (!tgtoActive(view)) return;
  $('tgtoSearchTools').hidden = view.mode !== 'search';
  $('modalBody').querySelectorAll('[data-tgto-tab]').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-tgto-tab') === (view.mode === 'detail' ? 'history' : view.mode));
  });
}
function tgtoResources(){
  var view = tgtoView;
  if (!view || view.busy) return;
  view.mode = 'search'; tgtoTabs(view);
  if (!view.searched) tgtoSearch(); else tgtoRenderResources(view);
}
var tgtoFacetLabels = {source:'资源来源',provider:'资源类型',resolution:'分辨率',release_type:'视频类型',video_effect:'视频效果',release_group:'发布组'};
function tgtoSelectFacet(key, index){
  var view = tgtoView;
  if (!view || view.busy || view.mode !== 'search') return;
  view.filters[key] = index < 0 ? '' : view.facets[key][index];
  tgtoRenderResources(view);
}
function tgtoRenderResources(view){
  if (!tgtoActive(view) || view.mode !== 'search') return;
  if (!view.facetsReady) { $('tgtoFilterPanel').open = !(window.innerWidth < 600); view.facetsReady = true; }
  view.facets = {};
  var facets = Object.keys(tgtoFacetLabels).map(function(key){
    var values = Array.from(new Set(view.items.map(function(x){return x[key] || '';}).filter(Boolean))).sort();
    view.facets[key] = values;
    if (!values.length) return '';
    // Counts are from returned candidates, never invented upstream totals.
    var options = [{value:'',label:'全部',index:-1}].concat(values.map(function(v,i){return {value:v,label:v,index:i};}));
    return '<div class="tgto-facet"><span class="tgto-facet-label">' + tgtoFacetLabels[key] + '</span><div>' + options.map(function(o){
      var count = view.items.filter(function(x){return (!o.value || x[key] === o.value) && Object.keys(view.filters).every(function(k){return k === key || !view.filters[k] || x[k] === view.filters[k];});}).length;
      return '<button type="button" aria-pressed="' + ((!view.filters[key] && !o.value) || view.filters[key] === o.value) + '" class="tgto-chip' + (((view.filters[key] || '') === o.value) ? ' selected' : '') + '" onclick="tgtoSelectFacet(' + jsarg(key) + ',' + o.index + ')">' + esc(o.label) + '<small>' + count + '</small></button>';
    }).join('') + '</div></div>';
  }).join('');
  $('tgtoFacets').innerHTML = facets;
  var visible = view.items.map(function(item,index){return {item:item,index:index};}).filter(function(x){
    return Object.keys(view.filters).every(function(k){return !view.filters[k] || x.item[k] === view.filters[k];});
  });
  var cards = visible.map(function(x){
    var item = x.item;
    var badges = [item.source,item.provider,item.size].filter(Boolean);
    var specs = [item.resolution || item.quality,item.release_type,item.video_effect,item.codec,item.audio,item.release_group ? '发布组：' + item.release_group : '',item.specs].filter(Boolean);
    var access = '<span class="tgto-points">' + esc(item.points !== '' && item.points != null ? item.points + ' 积分' : '积分未提供') + '</span>' +
      '<span class="tgto-unlock ' + (item.unlocked === true ? 'unlocked' : item.unlocked === false ? 'locked' : '') + '">' +
      (item.unlocked === true ? '当前账号已解锁' : item.unlocked === false ? '当前账号未解锁' : '账号解锁状态未知') + '</span>' +
      '<span class="tgto-unlock-count">' + esc(item.unlock_count !== '' && item.unlock_count != null ? '已解锁 ' + item.unlock_count + ' 人' : '解锁人数未提供') + '</span>';
    function detail(label, value){return value ? '<div class="tgto-resource-row"><span>' + label + '</span><p>' + esc(value) + '</p></div>' : '';}
    return '<article class="tgto-resource"><div class="tgto-resource-content"><h4>' + esc(item.title) + '</h4>' +
      '<div class="tgto-publisher">发布者：' + esc(item.publisher || '上游未提供') + '</div><div class="tgto-tags tgto-source-tags">' +
      Array.from(new Set(badges)).map(function(t){return '<span>' + esc(t) + '</span>';}).join('') + '</div>' +
      '<div class="tgto-tags tgto-access-tags">' + access + '</div>' +
      detail('评阅', item.reviewed_count !== '' && item.reviewed_count != null ? item.reviewed_count + ' 人' : '') +
      detail('规格', specs.join(' · ')) + detail('字幕', item.subtitles) + detail('校验', item.verification) +
      detail('文件', item.file_count ? item.file_count + ' 个文件' : '') + detail('备注', item.note) + '</div>' +
      '<div class="tgto-resource-actions">' +
      (item.transferable ? '<button class="btn tgto-transfer" onclick="tgtoTransfer(' + x.index + ')">转存</button><button class="btn gray" onclick="tgtoCopyLink(' + x.index + ')">复制链接</button>' : '<span class="hint tgto-unavailable">缺少资源标识</span>') +
      '</div>' +
      (item.copyLink ? '<div class="tgto-copy-ready"><input readonly aria-label="资源分享链接" value="'+esc(item.copyLink)+'"><button class="btn gray" onclick="tgtoCopyReady('+x.index+')">复制已获取链接</button></div>' : '') + '</article>';
  }).join('');
  tgtoResult(view, '<div class="tgto-result-heading"><strong>可用资源 <span>' + visible.length + '</span></strong><span>已返回 ' + view.items.length + ' 条</span></div>' +
    (cards ? '<div class="tgto-resource-grid">' + cards + '</div>' : '<div class="tgto-empty"><strong>' + (view.items.length ? '当前筛选暂无资源' : '暂未找到资源') + '</strong><p>' + (view.items.length ? '切换为全部，查看其他版本。' : '可调整季集后重新搜索。') + '</p></div>'),
    view.truncated ? '仅返回前 50 条；筛选作用于这 50 条，可按季集重新搜索。' : '已加载候选资源；转存前请核对作品、版本与季集。');
}
function tgtoActive(view){
  var section = $('modalBody').querySelector('[data-tgto-view]');
  return tgtoView === view && $('modalBg').classList.contains('show') && section &&
    Number(section.getAttribute('data-tgto-view')) === view.generation;
}
async function tgtoOperation(action, payload){
  var view=tgtoView;if(!view||view.busy)return;
  view.operations=view.operations||{};
  var key=action+JSON.stringify(payload)+$('tgtoProvider').value.trim();
  var id=view.operations[key]||(view.operations[key]=tgtoOperationId());
  view.busy=true;
  try {
    $('tgtoStatus').textContent='正在提交上游操作，请勿重复操作…';
    var r=await api('/api/upstream/operations/'+action,{method:'POST',body:JSON.stringify(Object.assign({},payload,{confirmed:true,operation_id:id,provider:$('tgtoProvider').value.trim()})),timeoutMs:25000});
    var job=r.job;
    for(var n=0;job.state==='submitting' && n<25 && tgtoActive(view);n++){
      await new Promise(function(resolve){setTimeout(resolve,1000);});
      var jobs=await api('/api/upstream/jobs');job=jobs.items.find(function(x){return x.id===job.id;})||job;
    }
    if(tgtoActive(view))$('tgtoStatus').textContent=job.message;
    return job;
  }catch(e){if(tgtoActive(view))$('tgtoStatus').textContent='结果未确认：'+e.message+'。请到上游核实，不会自动重试。';}
  finally{view.busy=false;}
}
async function tgtoCopyLink(index){
  var view=tgtoView,item=view&&view.items[index];if(!item||view.busy)return;
  if(!confirm('通过上游获取此资源链接；未解锁资源可能按上游规则扣积分。确认继续？'))return;
  var job=await tgtoOperation('copy_link',{handle:item.handle});
  if(job&&job.state==='submitted'&&tgtoActive(view)){
    try{var r=await api('/api/upstream/jobs/'+encodeURIComponent(job.id)+'/link');if(!tgtoActive(view))return;item.copyLink=r.link;tgtoRenderResources(view);$('tgtoStatus').textContent='链接已获取，点击“复制已获取链接”；也可长按链接框手动复制。';}
    catch(e){if(tgtoActive(view))$('tgtoStatus').textContent=e.message;}
  }
}
async function tgtoCopyReady(index){
  var item=tgtoView&&tgtoView.items[index];var link=item&&item.copyLink;if(!link)return;
  try{await navigator.clipboard.writeText(link);toast('链接已复制','success');}
  catch(e){$('tgtoStatus').textContent='浏览器未允许剪贴板访问，请长按链接框手动复制。';}
}
async function tgtoSubscribeResource(){
  var view=tgtoView;if(!view||view.busy)return;
  if(!confirm('启用上游资源自动订阅《'+view.seed.title+'》，并登记 Emby 入库通知？\n上游按其配置监控、获取和整理资源，可能扣积分。请先确认上游资源规则、积分限制和保存目录，以及 StrimKeep Telegram / 入库监控已配置。'))return;
  await tgtoOperation('resource_subscribe',{title:view.seed.title,media_type:view.seed.media_type,tmdb_id:String(view.seed.tmdb_id||''),notify_import:true});
}
async function tgtoResourceSubscriptions(){
  var view=tgtoView;if(!view||view.busy)return;
  view.mode='subscriptions';tgtoTabs(view);
  try{var r=await api('/api/upstream/resource-subscriptions');var notification=r.notifications||{};tgtoResult(view,'<button class="btn gray" onclick="tgtoSubscriptionEvents()">查看上游监控事件</button><p class="hint">已登记 '+esc(notification.count||0)+' 部入库通知，待发送 '+esc(notification.pending||0)+' 条</p>'+r.items.map(function(x){return '<article class="tgto-resource"><h4>'+esc(x.title)+'</h4><p>'+esc(x.enabled===true?'上游已启用':x.enabled===false?'上游已停用':'状态未提供')+' · 目标 '+esc(x.target_provider||'未提供')+'</p></article>';}).join(''),'上游负责资源监控与获取，入库通知以 Emby 确认的数据为准。');}
  catch(e){tgtoResult(view,'',e.message);}
}
async function tgtoSubscriptionEvents(){
  var view=tgtoView;if(!view||view.busy)return;
  try{var r=await api('/api/upstream/subscription-events');tgtoResult(view,'<button class="btn gray" onclick="tgtoResourceSubscriptions()">返回资源订阅</button>'+r.items.map(function(x){return '<article class="tgto-resource"><h4>'+esc(x.title)+'</h4><p>'+esc(x.status)+'</p><p>'+esc(x.message)+'</p></article>';}).join(''),'上游最近 30 条监控 / 资源处理事件');}
  catch(e){tgtoResult(view,'',e.message);}
}
function tgtoResult(view, html, status){
  if (!tgtoActive(view)) return;
  $('tgtoResults').innerHTML = html;
  $('tgtoStatus').textContent = status || '';
}
async function tgtoSearch(){
  var view = tgtoView;
  if (!view || view.busy) return;
  view.busy = true; view.items = []; view.mode = 'search'; view.searched = true; view.filters = {}; view.facetsReady = false;
  tgtoTabs(view); $('tgtoFacets').innerHTML = '';
  var body = Object.assign({}, view.seed);
  if ($('tgtoSeason')) { body.season = $('tgtoSeason').value; body.episode = $('tgtoEpisode').value; }
  $('tgtoSearchBtn').disabled = true;
  tgtoResult(view, '<div class="tgto-skeleton" aria-hidden="true"><i></i><i></i><i></i><i></i></div>', '正在查询上游资源…');
  try {
    var r = await api('/api/upstream/search', {method:'POST', body:JSON.stringify(body), timeoutMs:50000});
    view.items = r.items; view.truncated = r.truncated;
    tgtoRenderResources(view);
  } catch(e){ tgtoResult(view, '<div class="tgto-empty"><strong>资源搜索未完成</strong><p>检查上游连接后，可点击重新搜索。</p></div>', '资源搜索失败：' + e.message); }
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
  tgtoTabs(view);
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
    '这里显示上游操作提交结果；实际资源处理进度请查看上游，片库状态以 Emby 入库为准。');
    if (r.items.some(function(job){return job.state === 'submitting';}) && !view.jobPollPending) {
      view.jobPollPending = true;
      setTimeout(function(){ view.jobPollPending = false; if (tgtoActive(view)) tgtoJobs(view); }, 1500);
    }
  } catch(e){ if (tgtoActive(view) && view.mode === 'jobs') $('tgtoStatus').textContent = '任务查询失败：' + e.message + '；可手动重新查询'; }
  finally { view.jobsLoading = false; }
}
async function tgtoAcknowledge(id){
  if (!confirm('请先在上游核实此操作。\n清除本地记录不会取消上游任务，但会解除重复提交保护。继续？')) return;
  try {
    await api('/api/upstream/jobs/' + encodeURIComponent(id) + '/acknowledge', {method:'POST',body:'{"confirmed":true}'});
    await tgtoJobs();
  } catch(e){ toast(e.message,'error'); }
}
async function tgtoHistory(page){
  var view = tgtoView;
  if (!view || view.busy) return;
  view.historyPage = page; view.busy = true; view.mode = 'history';
  tgtoTabs(view);
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
