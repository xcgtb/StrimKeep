/* 115 account-bound identities. No background network scans or token readback. */
var cloud115View={account:'',generation:0,trail:[],parent:'0',next:null,items:[],busy:false};
var cloud115Selection=null;
var cloud115SettingsGeneration=0;
var cloud115QrGeneration=0;
var cloud115ReportedTasks={};
var cloud115OrganizeSelection=null;
function cloud115StateLabel(value){
  return ({active:'身份已绑定',generating:'生成中 / 待核实',needs_review:'需要核实',remote_deleted:'云端已删，STRM 待清理',deleted:'清理完成',submitted:'已记录删除请求',unknown:'删除结果待核实',confirmed:'已确认源文件消失'})[value]||value;
}
async function loadCloud115(){
  if(!$('cloud115Status'))return;
  var generation=++cloud115SettingsGeneration;
  try{
    var r=await api('/api/cloud115/status');
    if(generation!==cloud115SettingsGeneration)return;
    $('cloud115Strict').checked=!!r.require_binding;
    $('cloud115Base').value=r.playback_base||'';
    var selected=$('cloud115Account').value;
    $('cloud115Account').innerHTML='<option value="">选择已授权账号</option>'+r.accounts.map(function(a){return '<option value="'+esc(a.id)+'" '+(!a.enabled?'disabled':'')+'>'+esc(a.name)+' · '+esc(a.uid)+(a.enabled?'':' · 需重新授权')+'</option>';}).join('');
    if(r.accounts.some(function(a){return a.id===selected&&a.enabled;}))$('cloud115Account').value=selected;
    else if(r.accounts.length===1&&r.accounts[0].enabled)$('cloud115Account').value=r.accounts[0].id;
    if(cloud115View.account!==$('cloud115Account').value)cloud115ResetBrowser();
    $('cloud115Bindings').innerHTML=r.bindings.length?r.bindings.map(function(b){return '<div class="list-item"><div><b>'+esc(cloud115StateLabel(b.state))+'</b><div class="hint" style="overflow-wrap:anywhere">'+esc(b.strm_path)+'</div><div class="hint">115 文件 ID：'+esc(b.file_id)+'</div>'+(b.state!=='deleted'?'<button class="btn gray" onclick="cloud115Verify('+jsarg(b.id)+')">仅复核身份 / 删除结果</button>':'')+'</div></div>';}).join(''):'<p class="hint">尚无绑定。已有 STRM 不会自动按名称接管。</p>';
    $('cloud115Deletions').innerHTML=r.deletions.length?r.deletions.map(function(d){return '<div class="list-item"><div><b>'+esc(cloud115StateLabel(d.state))+'</b><div class="hint">文件 ID：'+esc(d.file_id)+'</div><div class="hint">'+esc(d.message||'')+'</div></div></div>';}).join(''):'<p class="hint">尚无官方删除记录</p>';
    var plans=r.organize_plans||[];
    $('cloud115OrganizeRecords').innerHTML=plans.length?plans.map(function(p){return '<div class="list-item"><div><b>'+esc(({preview:'待确认整理',running:'执行中',unknown:'整理结果待核实',awaiting_resume:'可继续未提交步骤',done:'整理完成',cancelled:'未执行计划已取消'})[p.state]||p.state)+'</b><div class="hint">文件 ID '+esc(p.file_id)+' → 目录 ID '+esc(p.target_parent)+'</div><div class="hint">'+esc(p.message||'')+'</div>'+(p.state!=='done'&&p.state!=='cancelled'?'<button class="btn gray" onclick="cloud115OpenOrganizePlan('+jsarg(p.id)+')">查看预览</button>':'')+(p.state==='unknown'||p.state==='running'?'<button class="btn gray" onclick="cloud115VerifyOrganize('+jsarg(p.id)+')">仅复核整理结果</button>':'')+'</div></div>';}).join(''):'<p class="hint">尚无整理记录</p>';
    $('cloud115Status').textContent=r.accounts.length?'官方接口接入已配置；真实权限与播放仍需验证。':'先添加官方授权账号。不会自动访问网盘。';
    if(typeof cloud115RenderTransfers==='function')cloud115RenderTransfers(r.transfer_plans||[]);
  }catch(e){$('cloud115Status').textContent='读取失败：'+e.message;}
}
async function cloud115SaveAccount(){
  var button=$('cloud115AccountBtn');if(button.disabled)return;button.disabled=true;
  cloud115QrGeneration++;$('cloud115Qr').innerHTML='';
  $('cloud115Status').textContent='正在向 115 验证账号身份…';
  try{
    await api('/api/cloud115/accounts',{method:'POST',body:JSON.stringify({name:$('cloud115Name').value,access_token:$('cloud115Access').value.trim(),refresh_token:$('cloud115Refresh').value.trim()}),timeoutMs:25000});
    $('cloud115Access').value='';$('cloud115Refresh').value='';
    await loadCloud115();toast('115 身份已验证并保存','success');
  }catch(e){$('cloud115Status').textContent='保存失败：'+e.message;}
  finally{button.disabled=false;}
}
async function cloud115StartQr(){
  var button=$('cloud115QrBtn');if(button.disabled)return;button.disabled=true;
  var generation=++cloud115QrGeneration;
  $('cloud115QrStatus').textContent='正在申请官方授权二维码…';$('cloud115Qr').innerHTML='';
  try{
    var r=await api('/api/cloud115/authorization/start',{method:'POST',body:JSON.stringify({name:$('cloud115Name').value,app_id:$('cloud115AppId').value.trim()}),timeoutMs:25000});
    if(generation!==cloud115QrGeneration)return;
    $('cloud115Qr').innerHTML='<img style="width:100%;background:white;border-radius:8px" alt="115 官方授权二维码" src="/api/cloud115/authorization/'+encodeURIComponent(r.session_id)+'/qr.svg">';
    $('cloud115QrStatus').textContent='请用 115 App 扫码并核对授权应用。二维码最多保留 5 分钟。';
    cloud115PollQr(r.session_id,generation,r.expires_at);
  }catch(e){if(generation===cloud115QrGeneration)$('cloud115QrStatus').textContent=e.message;}
  finally{button.disabled=false;}
}
async function cloud115PollQr(session,generation,expires){
  var panel=$('cloud115AuthPanel');
  if(generation!==cloud115QrGeneration)return;
  if(Date.now()/1000>expires){$('cloud115QrStatus').textContent='二维码已过期，请重新生成';$('cloud115Qr').innerHTML='';return;}
  if(document.hidden||!panel||!panel.open){setTimeout(function(){cloud115PollQr(session,generation,expires);},3000);return;}
  try{
    var r=await api('/api/cloud115/authorization/'+encodeURIComponent(session),{method:'POST',body:'{}',timeoutMs:25000});
    if(generation!==cloud115QrGeneration)return;
    var messages={waiting:'等待扫码…',scanned:'已扫码，请在 115 App 确认授权',done:'授权完成，账号身份已保存',unknown:'授权结果待核实，请重新生成二维码',cancelled:'已取消授权',expired:'二维码已过期，请重新生成'};
    $('cloud115QrStatus').textContent=messages[r.state]||'授权状态未知';
    if(r.state==='done'){$('cloud115Qr').innerHTML='';loadCloud115();return;}
    if(r.state==='waiting'||r.state==='scanned')setTimeout(function(){cloud115PollQr(session,generation,expires);},3000);
  }catch(e){if(generation===cloud115QrGeneration)$('cloud115QrStatus').textContent='授权查询失败：'+e.message+'；可重新生成二维码。';}
}
async function cloud115SavePolicy(){
  cloud115SettingsGeneration++;
  try{await api('/api/cloud115/policy',{method:'POST',body:JSON.stringify({require_binding:$('cloud115Strict').checked,playback_base:$('cloud115Base').value})});toast('115 设置已保存','success');}
  catch(e){$('cloud115Status').textContent='保存失败：'+e.message;}
}
async function cloud115Adopt(){
  var button=$('cloud115AdoptBtn');if(button.disabled)return;
  var account=$('cloud115Account').value;
  if(!account){$('cloud115AdoptResult').textContent='请选择已授权账号';return;}
  button.disabled=true;
  try{
    var r=await api('/api/cloud115/adopt',{method:'POST',body:JSON.stringify({account_id:account,relative_path:$('cloud115AdoptPath').value})});
    $('cloud115AdoptResult').textContent='已提交核验，正在查询结果…';
    cloud115ReportedTasks.cloud115AdoptResult=r.task_id;
    cloud115ReportTask(r.task_id,'cloud115AdoptResult','cloud115AdoptBtn');
  }catch(e){$('cloud115AdoptResult').textContent=e.message;button.disabled=false;}
}
async function cloud115Verify(id){
  try{
    var r=await api('/api/cloud115/bindings/'+encodeURIComponent(id)+'/verify',{method:'POST',body:'{}'});
    $('cloud115Status').textContent='正在按原文件 ID 复核；不会重发删除或删除本地文件。';
    cloud115ReportedTasks.cloud115Status=r.task_id;
    cloud115ReportTask(r.task_id,'cloud115Status');
  }catch(e){$('cloud115Status').textContent=e.message;}
}
async function cloud115ReportTask(id,nodeId,buttonId){
  if(cloud115ReportedTasks[nodeId]!==id||!$(nodeId))return;
  try{
    var r=await api('/api/task/'+encodeURIComponent(id)),t=r.task||r;
    if(cloud115ReportedTasks[nodeId]!==id)return;
    if(t.status==='running'){setTimeout(function(){cloud115ReportTask(id,nodeId,buttonId);},1000);return;}
    await loadCloud115();
    if(cloud115ReportedTasks[nodeId]!==id)return;
    $(nodeId).textContent=t.status==='success'?((t.result||{}).message||'核验完成'):(t.error||'任务失败，请查看运行日志');
    if(buttonId&&$(buttonId))$(buttonId).disabled=false;
  }catch(e){if(cloud115ReportedTasks[nodeId]===id){$(nodeId).textContent='结果查询失败：'+e.message+'；请先刷新绑定记录，勿重复提交。';if(buttonId&&$(buttonId))$(buttonId).disabled=false;}}
}
function cloud115ResetBrowser(){
  cloud115Selection=null;
  cloud115OrganizeSelection=null;
  if(typeof cloud115ResetShare==='function')cloud115ResetShare();
  cloud115View.generation++;cloud115View.account=$('cloud115Account').value;cloud115View.trail=[];cloud115View.items=[];cloud115View.parent='0';cloud115View.next=null;
  $('cloud115Files').innerHTML='';$('cloud115Breadcrumb').textContent='';$('cloud115Next').hidden=true;
}
async function cloud115Files(parent,offset,name,previousTrail){
  var account=$('cloud115Account').value;
  if(!account){$('cloud115Status').textContent='请选择已授权账号';return;}
  var generation=++cloud115View.generation;
  var trail=(previousTrail||cloud115View.trail).slice();
  if(name)trail.push({id:parent,name:name});
  else if(parent==='0')trail=[];
  $('cloud115Status').textContent='正在读取选定目录…';
  try{
    var r=await api('/api/cloud115/accounts/'+encodeURIComponent(account)+'/files?parent_id='+encodeURIComponent(parent)+'&offset='+offset,{timeoutMs:50000});
    if(generation!==cloud115View.generation||account!==$('cloud115Account').value)return;
    cloud115View.account=account;cloud115View.trail=trail;cloud115View.parent=parent;cloud115View.next=r.next_offset;cloud115View.items=r.items;
    $('cloud115Breadcrumb').textContent='根目录'+(trail.length?' / '+trail.map(function(x){return x.name;}).join(' / '):'');
    $('cloud115Files').innerHTML=r.items.map(function(f,i){return '<div class="list-item" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><div style="flex:1;min-width:0"><b style="overflow-wrap:anywhere">'+esc(f.name)+'</b><div class="hint">'+(f.is_dir?'目录':'文件')+' · ID '+esc(f.id)+'</div></div><button class="btn gray" onclick="cloud115Choose('+i+')">'+(f.is_dir?'进入':'生成 STRM')+'</button>'+(!f.is_dir?'<button class="btn gray" onclick="cloud115OrganizeChoose('+i+')">整理预览</button>':'')+'</div>';}).join('')||'<p class="hint">此页没有文件</p>';
    $('cloud115Next').hidden=r.next_offset===null;$('cloud115Status').textContent='目录已读取；生成会核对文件 ID，不覆盖已有 STRM。';
  }catch(e){if(generation===cloud115View.generation)$('cloud115Status').textContent='目录读取失败：'+e.message;}
}
function cloud115Parent(){
  if(!cloud115View.trail.length)return cloud115Files('0',0);
  var trail=cloud115View.trail.slice();trail.pop();var last=trail[trail.length-1];cloud115Files(last?last.id:'0',0,null,trail);
}
function cloud115More(){if(cloud115View.next!==null)cloud115Files(cloud115View.parent,cloud115View.next);}
function cloud115Choose(index){
  var f=cloud115View.items[index];if(!f)return;
  if(f.is_dir)return cloud115Files(f.id,0,f.name);
  cloud115Selection={file:f,account:cloud115View.account,task:null};
  var relative=cloud115View.trail.map(function(x){return x.name;}).concat(f.name.replace(/\.[^.]+$/,'.strm')).join('/');
  openModal('<h3>生成并绑定 115 STRM</h3><p>'+esc(f.name)+'</p><p class="hint">使用已整理的视频与目录。本步骤不会移动、改名或转存。已登记转存关联文件自动沿用；其余仅按明确选择的 ID 登记。</p><div class="form-row"><label for="cloud115Relative">本地库内相对路径</label><input id="cloud115Relative" value="'+esc(relative)+'" maxlength="2048"></div><div class="form-row"><label for="cloud115Sidecars">关联字幕 / 元数据 ID（可选，逗号分隔）</label><input id="cloud115Sidecars" autocomplete="off" placeholder="仅填写确定属于该视频的文件 ID"></div><p class="hint">这些关联文件会纳入本地库治理源文件删除，请核对归属。</p><p id="cloud115GenerateResult" class="hint" role="status"></p><div class="settings-actions"><button class="btn" id="cloud115GenerateBtn" onclick="cloud115Generate('+index+')">生成并登记身份</button><button class="btn gray" onclick="closeModal()">关闭</button></div>');
}
async function cloud115Generate(index){
  var selection=cloud115Selection, f=selection&&selection.file,button=$('cloud115GenerateBtn');if(!f||!button||button.disabled)return;
  button.disabled=true;
  try{
    var raw=$('cloud115Sidecars').value.trim();
    var r=await api('/api/cloud115/generate',{method:'POST',body:JSON.stringify({account_id:selection.account,file_id:f.id,relative_path:$('cloud115Relative').value,sidecar_ids:raw?raw.split(',').map(function(x){return x.trim();}):[]})});
    if(cloud115Selection!==selection||!$('cloud115GenerateResult'))return;
    selection.task=r.task_id;
    $('cloud115GenerateResult').textContent='任务已提交，正在复核文件并生成…';
    cloud115WatchTask(r.task_id);
  }catch(e){if(cloud115Selection===selection&&$('cloud115GenerateResult'))$('cloud115GenerateResult').textContent=e.message;button.disabled=false;}
}
async function cloud115WatchTask(id){
  var selection=cloud115Selection;
  if(!selection||selection.task!==id||!$('cloud115GenerateResult'))return;
  try{
    var r=await api('/api/task/'+encodeURIComponent(id));var t=r.task||r;
    if(cloud115Selection!==selection||!$('cloud115GenerateResult'))return;
    if(t.status==='running'){
      if($('cloud115GenerateResult'))setTimeout(function(){cloud115WatchTask(id);},1000);
      return;
    }
    if($('cloud115GenerateResult'))$('cloud115GenerateResult').textContent=t.status==='success'?'STRM 已生成并登记身份。请到 Emby 确认入库。':(t.error||'任务未成功，请查看运行日志');
    if($('cloud115GenerateBtn'))$('cloud115GenerateBtn').disabled=false;
    loadCloud115();
  }catch(e){if(cloud115Selection===selection&&$('cloud115GenerateResult'))$('cloud115GenerateResult').textContent='查询失败：'+e.message+'；请勿重复提交，先刷新绑定记录。';}
}

function cloud115OrganizeChoose(index){
  var file=cloud115View.items[index];if(!file||file.is_dir)return;
  cloud115OrganizeSelection={file:file,account:cloud115View.account,parent:cloud115View.parent,task:null,plan:null};
  openModal('<h3>115 按 ID 整理预览</h3><p>'+esc(file.name)+' · ID '+esc(file.id)+'</p><p class="hint">先预览，再确认移动和改名。已有 STRM 地址保持有效；当前不会自动识别 TMDB 或创建云端目录。</p><div class="form-row"><label for="cloud115OrganizeName">视频名称（保留原扩展名）</label><input id="cloud115OrganizeName" maxlength="255" value="'+esc(file.name)+'"></div><div class="form-row"><label for="cloud115OrganizeParent">目标目录 ID（根目录为 0）</label><input id="cloud115OrganizeParent" inputmode="numeric" maxlength="30" value="'+esc(cloud115View.parent)+'"></div><div class="form-row"><label for="cloud115OrganizeSidecars">关联文件（每行：文件 ID | 新名称，名称可省略）</label><textarea id="cloud115OrganizeSidecars" rows="3" placeholder="11 | 剧名.S01E01.ass"></textarea></div><p id="cloud115OrganizeResult" class="hint" role="status"></p><div id="cloud115OrganizePreview"></div><div class="settings-actions"><button class="btn" id="cloud115OrganizeBtn" onclick="cloud115OrganizePreview()">仅预览</button><button class="btn gray" onclick="closeModal()">关闭</button></div>');
}
async function cloud115OrganizePreview(){
  var selection=cloud115OrganizeSelection,button=$('cloud115OrganizeBtn');if(!selection||button.disabled)return;
  button.disabled=true;
  try{
    var text=$('cloud115OrganizeSidecars').value.trim(),sidecars=text?text.split(/\n/).filter(function(x){return x.trim();}).map(function(line){var parts=line.split('|');if(parts.length>2)throw new Error('关联文件格式应为 ID | 新名称');return {file_id:parts[0].trim(),new_name:parts.length===2?parts[1].trim():null};}):[];
    var r=await api('/api/cloud115/organize/preview',{method:'POST',body:JSON.stringify({account_id:selection.account,file_id:selection.file.id,new_name:$('cloud115OrganizeName').value,target_parent:$('cloud115OrganizeParent').value.trim(),sidecars:sidecars})});
    if(cloud115OrganizeSelection!==selection)return;
    selection.task=r.task_id;$('cloud115OrganizeResult').textContent='正在核验原 ID 与目标目录，仅生成预览…';cloud115WatchOrganize(r.task_id,'preview');
  }catch(e){if(cloud115OrganizeSelection===selection)$('cloud115OrganizeResult').textContent=e.message;button.disabled=false;}
}
function cloud115RenderOrganizePlan(plan){
  var selection=cloud115OrganizeSelection;if(!selection)return;
  selection.plan=plan.plan_id;
  var cancellable=plan.state!=='done'&&plan.state!=='cancelled'&&(!plan.steps||plan.steps.every(function(s){return s.state==='pending';}));
  $('cloud115OrganizePreview').innerHTML='<div style="overflow-x:auto"><table><thead><tr><th>文件 ID</th><th>原名称 / 目录</th><th>新名称 / 目录</th></tr></thead><tbody>'+plan.files.map(function(f){return '<tr><td>'+esc(f.file_id)+'</td><td>'+esc(f.old_name)+'<br>目录 ID '+esc(f.old_parent)+'</td><td>'+esc(f.new_name)+'<br>目录 ID '+esc(f.new_parent)+'</td></tr>';}).join('')+'</tbody></table></div><p class="hint">执行前会再次复核；只提交未执行步骤，未知结果不会自动重发。</p>'+(cancellable?'<button class="btn gray" onclick="cloud115CancelOrganize()">取消未提交的计划</button>':'');
  var executable=!plan.state||plan.state==='preview'||plan.state==='awaiting_resume';
  $('cloud115OrganizeBtn').hidden=!executable;
  $('cloud115OrganizeBtn').textContent='确认按 ID 执行';$('cloud115OrganizeBtn').setAttribute('onclick','cloud115OrganizeExecute()');$('cloud115OrganizeBtn').disabled=false;
}
async function cloud115OrganizeExecute(){
  var selection=cloud115OrganizeSelection,button=$('cloud115OrganizeBtn');if(!selection||!selection.plan||button.disabled)return;
  button.disabled=true;
  try{
    var r=await api('/api/cloud115/organize/'+encodeURIComponent(selection.plan)+'/execute',{method:'POST',body:JSON.stringify({confirm:true})});
    if(cloud115OrganizeSelection!==selection)return;
    selection.task=r.task_id;$('cloud115OrganizeResult').textContent='已确认执行，正在按文件 ID 整理…';cloud115WatchOrganize(r.task_id,'execute');
  }catch(e){if(cloud115OrganizeSelection===selection)$('cloud115OrganizeResult').textContent=e.message;button.disabled=false;}
}
async function cloud115WatchOrganize(id,mode){
  var selection=cloud115OrganizeSelection;if(!selection||selection.task!==id||!$('cloud115OrganizeResult'))return;
  try{
    var r=await api('/api/task/'+encodeURIComponent(id)),task=r.task||r;
    if(cloud115OrganizeSelection!==selection||!$('cloud115OrganizeResult'))return;
    if(task.status==='running'){setTimeout(function(){cloud115WatchOrganize(id,mode);},1000);return;}
    $('cloud115OrganizeResult').textContent=task.status==='success'?((task.result||{}).message||'整理已完成'):(task.error||'整理未成功，请复核记录');
    if(mode==='preview'&&task.status==='success')cloud115RenderOrganizePlan(task.result);
    else if(mode==='execute'){$('cloud115OrganizeBtn').hidden=true;cloud115View.generation++;cloud115View.items=[];$('cloud115Files').innerHTML='';loadCloud115();}
    else $('cloud115OrganizeBtn').disabled=false;
  }catch(e){if(cloud115OrganizeSelection===selection)$('cloud115OrganizeResult').textContent='查询失败：'+e.message+'；先查看整理记录，请勿重复提交。';}
}
async function cloud115OpenOrganizePlan(id){
  var selection={plan:id,task:null};cloud115OrganizeSelection=selection;
  try{
    var plan=await api('/api/cloud115/organize/'+encodeURIComponent(id));
    if(cloud115OrganizeSelection!==selection)return;
    openModal('<h3>115 整理计划</h3><p id="cloud115OrganizeResult" class="hint" role="status"></p><div id="cloud115OrganizePreview"></div><div class="settings-actions"><button class="btn" id="cloud115OrganizeBtn" onclick="cloud115OrganizeExecute()">确认执行</button><button class="btn gray" onclick="closeModal()">关闭</button></div>');
    $('cloud115OrganizeResult').textContent=plan.message;cloud115RenderOrganizePlan(plan);
  }catch(e){if(cloud115OrganizeSelection===selection)$('cloud115Status').textContent=e.message;}
}
async function cloud115VerifyOrganize(id){
  try{
    var r=await api('/api/cloud115/organize/'+encodeURIComponent(id)+'/verify',{method:'POST',body:'{}'});
    cloud115ReportedTasks.cloud115Status=r.task_id;$('cloud115Status').textContent='正在只读复核整理结果…';cloud115ReportTask(r.task_id,'cloud115Status');
  }catch(e){$('cloud115Status').textContent=e.message;}
}
async function cloud115CancelOrganize(){
  var selection=cloud115OrganizeSelection;if(!selection||!selection.plan)return;
  try{
    var r=await api('/api/cloud115/organize/'+encodeURIComponent(selection.plan)+'/cancel',{method:'POST',body:'{}'});
    if(cloud115OrganizeSelection!==selection)return;
    $('cloud115OrganizeResult').textContent=r.message;$('cloud115OrganizeBtn').hidden=true;$('cloud115OrganizePreview').innerHTML='';loadCloud115();
  }catch(e){if(cloud115OrganizeSelection===selection)$('cloud115OrganizeResult').textContent=e.message;}
}
