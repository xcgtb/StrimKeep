/* ═══════════ 目录清理 ═══════════ */
var __emptyDirsPreview = [];
var __emptyDirsScanSeq = 0;
var __emptyDirBrowser = {seq:0, data:null};
var __emptyDirsBusy = false;
function setEmptyDirsBusy(busy, operation){
  __emptyDirsBusy = busy;
  $('emptyDirChooseBtn').disabled = busy;
  $('cleanEmptyBtn').disabled = busy;
  $('emptyDirScanBtn').disabled = busy || (!$('emptyDirPath').value && $('emptyDirScope').value !== 'all');
  $('emptyDirScanBtn').textContent = busy && operation === 'scan' ? '扫描中…' : '预览扫描';
  $('cleanEmptyBtn').textContent = busy && operation === 'clean' ? '清理中…' : '执行清理';
}
function openEmptyDirPicker(){
  if (__emptyDirsBusy) return;
  openModal('<h3>选择扫描范围</h3><p style="font-size:12px;color:var(--text-dim)">可扫描本地与分享两库，或选择单个库及其子目录。</p><div id="emptyDirBrowserBody"></div>');
  loadEmptyDirBrowser($('emptyDirPath').value, 0, '');
}
async function loadEmptyDirBrowser(path, offset, search){
  var seq = ++__emptyDirBrowser.seq;
  __emptyDirBrowser.data = null;
  var box = $('emptyDirBrowserBody');
  if (!box) return;
  box.innerHTML = '<div class="list-empty">正在读取文件夹…</div>';
  try {
    var url = '/api/wash/empty-dirs/browse?path=' + encodeURIComponent(path || '') + '&offset=' + (offset || 0) + '&search=' + encodeURIComponent(search || '');
    var r = await api(url);
    if (seq !== __emptyDirBrowser.seq || !$('emptyDirBrowserBody') || !$('modalBg').classList.contains('show')) return;
    if (r.status !== 'success') throw new Error(r.message || '目录读取失败');
    __emptyDirBrowser.data = r;
    renderEmptyDirBrowser(r);
  } catch(e){
    if (seq !== __emptyDirBrowser.seq || !$('emptyDirBrowserBody')) return;
    $('emptyDirBrowserBody').innerHTML = '<div class="list-empty">' + esc(e.message) + '</div><button class="btn gray" onclick="loadEmptyDirBrowser(\'\',0,\'\')">返回扫描范围</button>';
  }
}
function renderEmptyDirBrowser(r){
  var allAvailable = (r.roots || []).length === 2 && r.roots.every(function(root){return root.available;});
  var html = '<div class="dir-picker-row"><button class="btn ' + (!r.path ? '' : 'gray') + '" onclick="loadEmptyDirBrowser(\'\',0,\'\')"' + (allAvailable ? '' : ' disabled') + '>整个媒体库</button>' + (r.roots || []).map(function(root, i){
    return '<button class="btn ' + (root.lib === r.lib ? '' : 'gray') + '" onclick="openEmptyDirRoot(' + i + ')"' + (root.available ? '' : ' disabled') + '>' + esc(root.name) + (root.available ? '' : '（不可访问）') + '</button>';
  }).join('') + '</div>';
  if (!r.path){
    html += '<p style="font-size:12px;color:var(--text-dim)">扫描范围：整个媒体库（本地＋分享）</p>';
    (r.roots || []).forEach(function(root){html += '<p style="font-size:12px;color:var(--text-dim);overflow-wrap:anywhere">' + esc(root.path) + (root.available ? '' : '：' + esc(root.message)) + '</p>';});
  } else {
    html += '<nav class="dir-picker-row" aria-label="当前位置"><span style="font-size:12px;color:var(--text-dim)">当前位置：</span>';
    var crumbs = r.breadcrumbs || [];
    if (crumbs.length > 1) html += '<button class="btn gray" onclick="openEmptyDirCrumb(0)">库根目录</button>';
    html += crumbs.slice(1).map(function(crumb, index){
      var i = index + 1;
      return '<span aria-hidden="true">›</span>' + (i === crumbs.length - 1 ? '<span aria-current="location">' + esc(crumb.name) + '</span>' : '<button class="btn gray" onclick="openEmptyDirCrumb(' + i + ')">' + esc(crumb.name) + '</button>');
    }).join('') + (crumbs.length > 1 ? '' : '<span aria-current="location">库根目录</span>') + '</nav><div style="font-size:12px;color:var(--text-dim);overflow-wrap:anywhere">目录路径：' + esc(r.path) + '</div>';
    html += '<div class="dir-picker-row"><input id="emptyDirBrowserFilter" placeholder="筛选当前层文件夹" value="' + esc(r.search || '') + '" onkeydown="if(event.key===\'Enter\') filterEmptyDirBrowser()"><button class="btn gray" onclick="filterEmptyDirBrowser()">筛选</button></div>';
    html += (r.items || []).map(function(item, i){
      return '<button class="dir-picker-item" onclick="openEmptyDirChild(' + i + ')"><span>📁 ' + esc(item.name) + '</span><span aria-hidden="true">›</span></button>';
    }).join('') || '<div class="list-empty">' + (r.search ? '没有匹配的子目录，可清空筛选' : '没有子目录，可选择当前目录') + '</div>';
    if (r.total){
      html += '<div style="font-size:12px;color:var(--text-dim);margin-top:10px">显示 ' + (r.items.length ? r.offset + 1 : 0) + '–' + (r.offset + r.items.length) + ' / ' + r.total + ' 个文件夹</div>';
    }
    if (r.offset || r.has_more){
      html += '<div class="dir-picker-row"><button class="btn gray" onclick="pageEmptyDirBrowser(-1)"' + (r.offset ? '' : ' disabled') + '>上一页</button><button class="btn gray" onclick="pageEmptyDirBrowser(1)"' + (r.has_more ? '' : ' disabled') + '>下一页</button></div>';
    }
  }
  html += '<p style="font-size:12px;color:var(--text-dim)">确认后返回页面，再点击「预览扫描」。</p><div class="dir-picker-footer"><button class="btn gray" onclick="closeModal()">取消</button><button class="btn" onclick="useEmptyDirSelection()"' + (!r.path && !allAvailable ? ' disabled' : '') + '>确认选择</button></div>';
  $('emptyDirBrowserBody').innerHTML = html;
}
function openEmptyDirRoot(i){ var r = __emptyDirBrowser.data; if (r && r.roots[i] && r.roots[i].available) loadEmptyDirBrowser(r.roots[i].path,0,''); }
function openEmptyDirChild(i){ var r = __emptyDirBrowser.data; if (r && r.items[i]) loadEmptyDirBrowser(r.items[i].path,0,''); }
function openEmptyDirCrumb(i){ var r = __emptyDirBrowser.data; if (r && r.breadcrumbs[i]) loadEmptyDirBrowser(r.breadcrumbs[i].path,0,''); }
function filterEmptyDirBrowser(){ var r = __emptyDirBrowser.data; if (r) loadEmptyDirBrowser(r.path,0,$('emptyDirBrowserFilter').value.trim()); }
function pageEmptyDirBrowser(direction){ var r = __emptyDirBrowser.data; if (r) loadEmptyDirBrowser(r.path,Math.max(0,r.offset + direction*r.limit),r.search); }
function useEmptyDirSelection(){
  if (__emptyDirsBusy) return;
  var r = __emptyDirBrowser.data;
  if (!r || (!r.path && (!(r.roots || []).length || r.roots.some(function(root){return !root.available;})))) return;
  $('emptyDirScope').value = r.path ? 'directory' : 'all';
  $('emptyDirPath').value = r.path || '';
  $('emptyDirSelectedPath').textContent = '扫描范围：' + (r.path || '整个媒体库（本地库＋分享库）');
  $('emptyDirChooseBtn').textContent = '更换扫描范围';
  $('emptyDirScanBtn').disabled = false;
  clearEmptyDirsPreview();
  if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '范围已选择，点击「预览扫描」开始。';
  closeModal();
}
function emptyDirFileSummary(x){
  if (!x.file_count) return '目录内没有文件';
  var exts = x.extensions || {}, groups = {字幕:0, 图片:0, NFO:0, 其他附属文件:0};
  Object.keys(exts).forEach(function(ext){
    var group = ['.srt','.ass','.ssa','.sub','.idx','.sup'].indexOf(ext)>=0 ? '字幕' :
      ['.jpg','.jpeg','.png','.gif','.webp'].indexOf(ext)>=0 ? '图片' : ext === '.nfo' ? 'NFO' : '其他附属文件';
    groups[group] += exts[ext];
  });
  return '附属文件 ' + x.file_count + ' 个 · ' + Object.keys(groups).filter(function(k){return groups[k];}).map(function(k){return k+' '+groups[k]+' 个';}).join(' · ');
}
function clearEmptyDirsPreview(){
  ++__emptyDirsScanSeq;
  __emptyDirsPreview = [];
  $('cleanEmptyBtn').style.display = 'none';
  $('emptyDirsStats').style.display = 'none';
  $('emptyDirsList').innerHTML = '<div class="list-empty">选择目录后点击「预览扫描」</div>';
}
async function scanEmptyDirs(){
  if (__emptyDirsBusy) return;
  var p = $('emptyDirPath').value.trim();
  var scope = $('emptyDirScope').value;
  if (!p && scope !== 'all') { openEmptyDirPicker(); return; }
  clearEmptyDirsPreview();
  var seq = __emptyDirsScanSeq;
  setEmptyDirsBusy(true, 'scan');
  if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '正在扫描 NAS，请等待预览结果…';
  $('emptyDirsList').innerHTML = '<div class="list-empty">正在扫描 NAS 所选范围…</div>';
  try {
    var r = await api('/api/wash/empty-dirs/scan', { method: 'POST', body: JSON.stringify({ path: p, scope: scope, limit: 100 }) });
    if (seq !== __emptyDirsScanSeq) return;
    if (r.status !== 'success') throw new Error(r.message || '扫描失败');
    if (p !== $('emptyDirPath').value.trim() || scope !== $('emptyDirScope').value) throw new Error('扫描目录已变化，请重新扫描');
    __emptyDirsPreview = r.preview || [];
    $('emptyDirsStats').style.display = '';
    $('emptyDirsStats').innerHTML = '<div class="ing-sum">' +
      '<span class="ing-chip">扫描文件夹 <b>' + (r.folders || 0) + '</b></span>' +
      '<span class="ing-chip">命中总数 <b>' + (r.hits || 0) + '</b></span>' +
      '<span class="ing-chip">残留目录命中 <b>' + (r.empty_dirs || 0) + '</b></span>' +
      '<span class="ing-chip">预览上限 <b>' + (r.preview_limit || 100) + '</b></span></div>';
    if (r.scopes) $('emptyDirsStats').innerHTML += '<div class="ing-sum">' + r.scopes.map(function(s){return '<span class="ing-chip">' + (s.lib === 'share' ? '分享库' : '本地库') + '命中 <b>' + s.hits + '</b></span>';}).join('') + '</div>';
    $('cleanEmptyBtn').style.display = (r.hits || 0) > 0 ? '' : 'none';
    var items = __emptyDirsPreview.map(function(x, i){
      return '<div class="ing-grp"><div style="padding:8px 12px;font-size:12px;line-height:1.8">' +
        '<b>' + (i + 1) + '</b> · <span class="ing-src">' + (x.lib === 'share' ? '分享库' : '本地库') + '</span> ' + esc(x.rel) + '<br>' +
        '<span style="color:var(--text-dim)">类型：' + esc(x.type) + ' · NAS：' + esc(x.abs_path) + (x.local_present === false ? '（已不存在）' : '') + '</span><br>' +
        (x.cloud_path ? '<span style="color:var(--text-dim)">115：' + esc(x.cloud_path) + (x.cloud_checked === false ? '（执行时通过 CD2 核对并清理）' : x.cloud_present ? '' : '（无对应目录）') + '</span><br>' : '') +
        (x.cloud_reason ? '<span style="color:var(--warn)">115 保留原因：' + esc(x.cloud_reason) + '</span><br>' : '') +
        (x.nas_file_count != null ? '<span style="color:var(--text-dim)">NAS 附属文件 ' + x.nas_file_count + ' 个' + (x.cloud_path ? x.cloud_checked === false ? ' · 115 删除数量在执行后显示' : ' · 115 附属文件 ' + (x.cloud_file_count || 0) + ' 个' : '') + '</span><br>' : '') +
        '<span style="color:var(--text-dim)">内容：' + esc(emptyDirFileSummary(x)) + '</span><br>' +
        '<span style="color:var(--text-dim)">规则：' + esc(x.rule) + '</span></div></div>';
    });
    $('emptyDirsList').innerHTML = items.length ? items.join('') : '<div class="list-empty">未发现目录残留</div>';
    if (r.hits > __emptyDirsPreview.length) {
      $('emptyDirsList').innerHTML += '<div class="list-empty">命中 ' + r.hits + ' 个，仅预览前 ' + r.preview_limit + ' 条；执行时将清理预览中的目录</div>';
    }
    toast('扫描完成：命中 ' + (r.hits || 0) + ' 个残留目录', 'success');
    if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '扫描完成：命中 ' + (r.hits || 0) + ' 项，当前预览 ' + __emptyDirsPreview.length + ' 项。' + (__emptyDirsPreview.length ? '可点击「执行清理」。' : '无需清理。');
  } catch(e){
    if (seq !== __emptyDirsScanSeq) return;
    $('emptyDirsList').innerHTML = '<div class="list-empty">' + esc(e.message) + '</div>';
    toast('扫描失败: ' + e.message, 'error');
    if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '扫描失败：' + e.message + '。可重新预览。';
  } finally {
    setEmptyDirsBusy(false);
  }
}

function showEmptyDirCleanupResult(r){
  var errors = r.errors || [];
  var heading = errors.length ? '清理结束：有未完成项目' : '清理完成';
  var failed = r.failed_count != null ? r.failed_count : errors.length;
  var summary = 'NAS：' + (r.nas_count || 0) + ' 个目录、' + (r.files_removed || 0) + ' 个文件；115：' + (r.cloud_count || 0) + ' 个目录、' + (r.cloud_files_removed || 0) + ' 个文件';
  openModal('<h3>' + esc(heading) + '</h3>' + (r.completed_at ? '<p style="font-size:12px;color:var(--text-dim)">' + esc(r.completed_at) + '</p>' : '') +
    '<p>' + esc(summary) + '</p><p>未完成 ' + failed + ' 项' + (r.already_absent ? ' · 已不存在 ' + r.already_absent + ' 项' : '') + '</p>' +
    (errors.length ? '<div style="max-height:240px;overflow:auto;font-size:12px;overflow-wrap:anywhere">' + errors.slice(0,50).map(function(msg){return '<p>' + esc(msg) + '</p>';}).join('') +
      (errors.length > 50 ? '<p>更多原因请查看计划存档。</p>' : '') + '</div>' : '') +
    (r.audit_error ? '<p style="color:var(--warn)">' + esc(r.audit_error) + '</p>' : '<p style="font-size:12px;color:var(--text-dim)">详情已保存到计划存档。</p>') +
    '<div class="dir-picker-row"><button class="btn gray" onclick="closeModal()">关闭</button>' +
    (r.audit_error ? '' : '<button class="btn" onclick="closeModal();switchTab(\'plans\')">查看计划存档</button>') + '</div>');
}
async function cleanEmptyDirs(){
  if (__emptyDirsBusy) return;
  if (!__emptyDirsPreview.length) { toast('请先预览扫描', 'error'); return; }
  if (!confirm('确定清理预览中的 ' + __emptyDirsPreview.length + ' 项残留？\n\n本地库会一起清理 NAS 和对应 115 的附属文件及空目录；分享库清理 NAS。\n不产生备份；115 有媒体或删除失败时跳过并显示原因。')) return;
  var seq = __emptyDirsScanSeq;
  setEmptyDirsBusy(true, 'clean');
  if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '正在清理 NAS 和对应 115，请等待结果…';
  try {
    var r = await api('/api/wash/empty-dirs/clean', { method: 'POST', body: JSON.stringify({ items: __emptyDirsPreview.map(function(x){ return {path:x.path, lib:x.lib, cloud_path:x.cloud_path || ''}; }) }) });
    if (r.status === 'busy') throw new Error(r.message || '有任务正在执行');
    if (r.status !== 'success') throw new Error(r.message || '清理失败');
    if (seq === __emptyDirsScanSeq) {
      clearEmptyDirsPreview();
      if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '需要继续清理时，点击「预览扫描」。';
    }
    showEmptyDirCleanupResult(r);
  } catch(e){
    toast('清理失败: ' + e.message, 'error');
    if ($('emptyDirTaskStatus')) $('emptyDirTaskStatus').textContent = '清理请求未完成：' + e.message + '。请重新预览确认当前状态。';
  } finally {
    setEmptyDirsBusy(false);
  }
}

var __cloudResiduePreview = null, __cloudResidueSeq = 0;
async function previewCloudResidue(i){
  var item = __emptyDirsPreview[i];
  if (!item) return;
  var seq = ++__cloudResidueSeq;
  __cloudResiduePreview = null;
  openModal('<h3>115 对应目录残留</h3><div id="cloudResidueBody"><div class="list-empty">正在核对云端目录与附属文件内容…</div></div>');
  try {
    var r = await api('/api/wash/cloud-residue/preview', {method:'POST',body:JSON.stringify({local_path:item.path})});
    if (seq !== __cloudResidueSeq || !$('cloudResidueBody') || !$('modalBg').classList.contains('show')) return;
    if (r.status !== 'success') throw new Error(r.message || '115 残留预览失败');
    __cloudResiduePreview = r;
    $('cloudResidueBody').innerHTML = '<p style="font-size:12px;overflow-wrap:anywhere">NAS 对应目录：' + esc(r.local_path) + '</p>' +
      '<p style="font-size:12px;overflow-wrap:anywhere">115 待清理目录：' + esc(r.cloud_path) + '</p>' +
      '<p><b>' + esc(emptyDirFileSummary(r)) + '</b></p>' +
      '<p style="font-size:12px;color:var(--text-dim)">确认后直接删除此 115 残留，不产生备份；失败项目会跳过并显示原因。NAS 字幕保留。预览有效期 10 分钟。</p>' +
      '<div style="max-height:240px;overflow:auto;font-size:12px;overflow-wrap:anywhere">' + (r.files || []).map(function(f){return '<div>' + esc(f) + '</div>';}).join('') +
      (r.files_truncated ? '<p>仅显示前 50 个文件；上方数量包含全部文件。</p>' : '') + '</div>' +
      '<div class="dir-picker-row"><button class="btn gray" onclick="closeModal()">关闭</button><button class="btn danger" id="cleanCloudResidueBtn" onclick="cleanCloudResidue()">清理此 115 残留</button></div>';
  } catch(e){
    if (seq !== __cloudResidueSeq || !$('cloudResidueBody')) return;
    $('cloudResidueBody').innerHTML = '<div class="list-empty">' + esc(e.message) + '</div><p style="font-size:12px;color:var(--text-dim)">115 源文件保留；不会仅凭本地没有 STRM 删除云端目录。</p><button class="btn gray" onclick="closeModal()">关闭</button>';
  }
}
async function cleanCloudResidue(){
  var r = __cloudResiduePreview;
  if (!r) return;
  if (!confirm('确认直接删除这个 115 残留目录？\n' + r.cloud_path + '\n' + emptyDirFileSummary(r) + '\n\n不产生备份；失败项目会跳过并显示原因。NAS 字幕保留。')) return;
  var btn = $('cleanCloudResidueBtn'), seq = __cloudResidueSeq;
  if (btn) btn.disabled = true;
  try {
    var result = await api('/api/wash/cloud-residue/clean', {method:'POST',body:JSON.stringify({token:r.token,confirmed:true})});
    if (result.status === 'partial') {
      if (seq === __cloudResidueSeq && $('cloudResidueBody')) {
        __cloudResiduePreview = null;
        $('cloudResidueBody').innerHTML = '<p>已删除 ' + (result.files_removed || 0) + ' 个附属文件，以下项目未完成：</p>' +
          (result.errors || []).slice(0,50).map(function(msg){return '<p style="font-size:12px;overflow-wrap:anywhere">' + esc(msg) + '</p>';}).join('') +
          '<p>请查看原因后重新预览。</p><button class="btn gray" onclick="closeModal()">关闭</button>';
      }
      toast('115 清理部分完成，已删除 ' + (result.files_removed || 0) + ' 个文件，请查看原因', 'info', 6000);
      return;
    }
    if (result.status !== 'success') throw new Error(result.message || '清理未完成');
    toast('已清理 115 残留：' + (result.files_removed || 0) + ' 个附属文件', 'success', 5000);
    if (seq === __cloudResidueSeq && $('cloudResidueBody')) {
      __cloudResiduePreview = null;
      $('cloudResidueBody').innerHTML = '<p>115 残留已清理，删除 ' + (result.files_removed || 0) + ' 个附属文件，无备份。</p><p style="font-size:12px">返回当前列表后，可执行本地残留清理。</p><button class="btn" onclick="closeModal()">返回本地残留列表</button>';
    }
  } catch(e){
    if (seq === __cloudResidueSeq && $('cloudResidueBody')) {
      __cloudResiduePreview = null;
      $('cloudResidueBody').innerHTML = '<div class="list-empty">' + esc(e.message) + '</div><p>请核对原因后重新预览。</p><button class="btn gray" onclick="closeModal()">关闭</button>';
    }
    toast('115 清理未完成: ' + e.message, 'error', 6000);
  }
}

