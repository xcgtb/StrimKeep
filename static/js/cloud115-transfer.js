/* Share source IDs and owned target IDs have distinct authorization scopes. */
var cloud115ShareView={generation:0,account:'',handle:null,parent:'0',next:null,items:[]};
var cloud115TransferSelection=null;
function cloud115ResetShare(){
  cloud115ShareView.generation++;cloud115ShareView.handle=null;cloud115ShareView.items=[];cloud115ShareView.next=null;cloud115TransferSelection=null;
  if($('cloud115ShareFiles'))$('cloud115ShareFiles').innerHTML='';if($('cloud115ShareNext'))$('cloud115ShareNext').hidden=true;
}
function cloud115RenderTransfers(rows){
  $('cloud115TransferRecords').innerHTML=rows.length?rows.map(function(p){return '<div class="list-item"><div><b>'+esc(({preview:'待确认转存',running:'转存中',unknown:'转存结果待核实',awaiting_resume:'可继续未提交步骤',done:'转存目标 ID 已核验',cancelled:'未转存计划已取消'})[p.state]||p.state)+'</b><div class="hint">分享源 ID '+esc(p.source_id)+' · 目标目录 ID '+esc(p.target_parent)+'</div><div class="hint">'+esc(p.message||'')+'</div><button class="btn gray" onclick="cloud115OpenTransfer('+jsarg(p.id)+')">查看身份与计划</button>'+(p.state==='unknown'||p.state==='running'?'<button class="btn gray" onclick="cloud115VerifyTransfer('+jsarg(p.id)+')">仅复核转存结果</button>':'')+'</div></div>';}).join(''):'<p class="hint">尚无独立转存记录</p>';
}
async function cloud115SaveCookie(){
  var button=$('cloud115CookieBtn'),account=$('cloud115Account').value,value=$('cloud115Cookie').value;
  if(button.disabled)return;if(!account){$('cloud115ShareStatus').textContent='先选择已通过官方身份验证的账号';return;}button.disabled=true;
  try{await api('/api/cloud115/transfer/cookie',{method:'POST',body:JSON.stringify({account_id:account,cookie:value}),timeoutMs:50000});if($('cloud115Cookie').value===value)$('cloud115Cookie').value='';loadCloud115();$('cloud115ShareStatus').textContent='辅助 Cookie 已验证为同一官方账号；不会回显。';}
  catch(e){$('cloud115ShareStatus').textContent=e.message;}finally{button.disabled=false;}
}
async function cloud115InspectShare(parent,offset){
  var account=$('cloud115Account').value;if(!account){$('cloud115ShareStatus').textContent='请选择官方账号，并保存同账号辅助 Cookie';return;}
  var generation=++cloud115ShareView.generation;cloud115ShareView.account=account;cloud115ShareView.handle=null;cloud115ShareView.items=[];$('cloud115ShareFiles').innerHTML='';$('cloud115ShareStatus').textContent='正在只读验证账号与分享目录…';
  try{var r=await api('/api/cloud115/transfer/inspect',{method:'POST',body:JSON.stringify({account_id:account,share_url:$('cloud115ShareUrl').value,parent_id:parent||'0',offset:offset||0})});if(generation===cloud115ShareView.generation)cloud115WatchShare(r.task_id,generation,account,parent||'0');}
  catch(e){if(generation===cloud115ShareView.generation)$('cloud115ShareStatus').textContent=e.message;}
}
async function cloud115WatchShare(id,generation,account,parent){
  if(generation!==cloud115ShareView.generation||account!==$('cloud115Account').value)return;
  try{
    var r=await api('/api/task/'+encodeURIComponent(id)),task=r.task||r;if(generation!==cloud115ShareView.generation||account!==$('cloud115Account').value)return;
    if(task.status==='running'){setTimeout(function(){cloud115WatchShare(id,generation,account,parent);},1000);return;}
    if(task.status!=='success'){$('cloud115ShareStatus').textContent=task.error||'分享读取失败';return;}
    var data=task.result;cloud115ShareView.handle=data.handle;cloud115ShareView.items=data.items;cloud115ShareView.parent=parent;cloud115ShareView.next=data.next_offset;
    $('cloud115ShareFiles').innerHTML=data.items.map(function(f,i){return '<div class="list-item" style="display:flex;gap:8px;align-items:center"><div style="flex:1;min-width:0"><b style="overflow-wrap:anywhere">'+esc(f.name)+'</b><div class="hint">分享源 ID '+esc(f.id)+' · '+(f.is_dir?'目录':String(f.size)+' 字节')+'</div></div><button class="btn gray" onclick="cloud115ShareChoose('+i+')">'+(f.is_dir?'打开目录':'转存预览')+'</button></div>';}).join('')||'<p class="hint">此页没有文件</p>';
    $('cloud115ShareNext').hidden=data.next_offset===null;$('cloud115ShareStatus').textContent='分享只读预览完成；转存需另行核对并确认。';
  }catch(e){if(generation===cloud115ShareView.generation)$('cloud115ShareStatus').textContent='任务查询失败：'+e.message;}
}
function cloud115ShareMore(){if(cloud115ShareView.next!==null)cloud115InspectShare(cloud115ShareView.parent,cloud115ShareView.next);}
function cloud115ShareChoose(index){
  var file=cloud115ShareView.items[index];if(!file||!cloud115ShareView.handle)return;if(file.is_dir)return cloud115InspectShare(file.id,0);
  cloud115TransferSelection={account:cloud115ShareView.account,handle:cloud115ShareView.handle,file:file,task:null,plan:null,result:null};
  openModal('<h3>115 独立分享转存</h3><p>'+esc(file.name)+' · 分享源 ID '+esc(file.id)+'</p><p class="hint">转存先进入任务独立暂存目录。核验本账号的新 ID 后，再整理和生成 STRM，不覆盖原有文件。</p><div class="form-row"><label for="cloud115TransferParent">本账号暂存父目录 ID（根目录为 0）</label><input id="cloud115TransferParent" inputmode="numeric" maxlength="30" value="'+esc($('cloud115TransferRoot').value||'0')+'"></div><div class="form-row"><label for="cloud115TransferSidecars">本页关联字幕 / 元数据的分享源 ID</label><input id="cloud115TransferSidecars" placeholder="逗号分隔，例如 101,102"></div><p class="hint">本次登记的关联关系会自动沿用到整理和 STRM，不按名称猜测。</p><p id="cloud115TransferResult" class="hint" role="status"></p><div id="cloud115TransferPreview"></div><div class="settings-actions"><button class="btn" id="cloud115TransferBtn" onclick="cloud115PreviewTransfer()">仅预览</button><button class="btn gray" onclick="closeModal()">关闭</button></div>');
}
async function cloud115PreviewTransfer(){
  var selection=cloud115TransferSelection,button=$('cloud115TransferBtn');if(!selection||button.disabled)return;button.disabled=true;
  try{var text=$('cloud115TransferSidecars').value.trim(),r=await api('/api/cloud115/transfer/preview',{method:'POST',body:JSON.stringify({handle:selection.handle,file_id:selection.file.id,target_parent:$('cloud115TransferParent').value.trim(),sidecar_ids:text?text.split(',').map(function(x){return x.trim();}):[]})});if(cloud115TransferSelection!==selection)return;selection.task=r.task_id;$('cloud115TransferResult').textContent='正在核验分享内容与目标目录，仅生成预览…';cloud115WatchTransfer(r.task_id,'preview');}
  catch(e){if(cloud115TransferSelection===selection)$('cloud115TransferResult').textContent=e.message;button.disabled=false;}
}
function cloud115RenderTransfer(plan){
  var selection=cloud115TransferSelection;if(!selection)return;selection.plan=plan.plan_id;selection.result=plan;var targets=plan.targets||[];
  $('cloud115TransferPreview').innerHTML='<p class="hint">官方账号 UID '+esc(plan.account_uid||'')+' · 分享码 '+esc(plan.share_code||'')+'<br>目标父目录 ID '+esc(plan.target_parent)+'<br>独立暂存目录：'+esc(plan.folder_name)+'</p><div style="overflow-x:auto"><table><thead><tr><th>分享源 ID</th><th>文件</th><th>本账号新 ID</th></tr></thead><tbody>'+plan.files.map(function(f){var target=targets.find(function(t){return t.source_id===f.source_id;});return '<tr><td>'+esc(f.source_id)+'</td><td>'+esc(f.name)+'</td><td>'+esc(target?target.file_id:'执行后核验')+'</td></tr>';}).join('')+'</tbody></table></div>'+(plan.state==='done'?'<p class="hint">新 ID 已核验；后续自动沿用本次登记的关联文件。</p><div class="settings-actions"><button class="btn gray" onclick="cloud115UseTransferTargets(\'organize\')">按 ID 整理视频</button><button class="btn" onclick="cloud115UseTransferTargets(\'strm\')">生成绑定 STRM</button></div>':'')+(plan.receive_state==='pending'&&plan.mkdir_state!=='submitted'&&plan.state!=='cancelled'?'<button class="btn gray" onclick="cloud115CancelTransfer()">取消尚未转存的计划</button>':'');
  $('cloud115TransferBtn').hidden=plan.state!=='preview'&&plan.state!=='awaiting_resume';$('cloud115TransferBtn').disabled=false;$('cloud115TransferBtn').textContent='确认独立转存';$('cloud115TransferBtn').setAttribute('onclick','cloud115ExecuteTransfer()');
}
async function cloud115ExecuteTransfer(){
  var selection=cloud115TransferSelection,button=$('cloud115TransferBtn');if(!selection||!selection.plan||button.disabled)return;button.disabled=true;
  try{var r=await api('/api/cloud115/transfer/'+encodeURIComponent(selection.plan)+'/execute',{method:'POST',body:JSON.stringify({confirm:true})});if(cloud115TransferSelection!==selection)return;selection.task=r.task_id;$('cloud115TransferResult').textContent='已确认转存，正在核验新文件 ID…';cloud115WatchTransfer(r.task_id,'execute');}
  catch(e){if(cloud115TransferSelection===selection)$('cloud115TransferResult').textContent=e.message;button.disabled=false;}
}
async function cloud115WatchTransfer(id,mode){
  var selection=cloud115TransferSelection;if(!selection||selection.task!==id||!$('cloud115TransferResult'))return;
  try{var r=await api('/api/task/'+encodeURIComponent(id)),task=r.task||r;if(cloud115TransferSelection!==selection||!$('cloud115TransferResult'))return;if(task.status==='running'){setTimeout(function(){cloud115WatchTransfer(id,mode);},1000);return;}
    $('cloud115TransferResult').textContent=task.status==='success'?(task.result.message||'预览已完成'):(task.error||'结果未确认，请复核原记录');
    if(task.status==='success')cloud115RenderTransfer(task.result);else if(mode==='execute')$('cloud115TransferBtn').hidden=true;else $('cloud115TransferBtn').disabled=false;if(mode==='execute')loadCloud115();
  }catch(e){if(cloud115TransferSelection===selection)$('cloud115TransferResult').textContent='任务查询失败：'+e.message+'；先复核原记录，勿重新转存。';}
}
async function cloud115OpenTransfer(id){
  var selection={plan:id,task:null,result:null};cloud115TransferSelection=selection;
  try{var plan=await api('/api/cloud115/transfer/'+encodeURIComponent(id));if(cloud115TransferSelection!==selection)return;selection.account=plan.account_id;
    openModal('<h3>115 转存身份与计划</h3><p id="cloud115TransferResult" class="hint" role="status"></p><div id="cloud115TransferPreview"></div><div class="settings-actions"><button class="btn" id="cloud115TransferBtn" onclick="cloud115ExecuteTransfer()">确认转存</button><button class="btn gray" onclick="closeModal()">关闭</button></div>');$('cloud115TransferResult').textContent=plan.message;cloud115RenderTransfer(plan);
  }catch(e){if(cloud115TransferSelection===selection)$('cloud115ShareStatus').textContent=e.message;}
}
async function cloud115VerifyTransfer(id){
  try{var r=await api('/api/cloud115/transfer/'+encodeURIComponent(id)+'/verify',{method:'POST',body:'{}'});cloud115ReportedTasks.cloud115Status=r.task_id;$('cloud115Status').textContent='正在只读复核原转存任务…';cloud115ReportTask(r.task_id,'cloud115Status');}
  catch(e){$('cloud115ShareStatus').textContent=e.message;}
}
async function cloud115CancelTransfer(){
  var selection=cloud115TransferSelection;if(!selection||!selection.plan)return;
  try{var r=await api('/api/cloud115/transfer/'+encodeURIComponent(selection.plan)+'/cancel',{method:'POST',body:'{}'});if(cloud115TransferSelection!==selection)return;$('cloud115TransferResult').textContent=r.message;cloud115RenderTransfer(r);loadCloud115();}
  catch(e){if(cloud115TransferSelection===selection)$('cloud115TransferResult').textContent=e.message;}
}
async function cloud115UseTransferTargets(mode){
  var selection=cloud115TransferSelection,plan=selection&&selection.result;if(!plan||plan.state!=='done'||!plan.targets.length)return;
  try{var r=await api('/api/cloud115/accounts/'+encodeURIComponent(plan.account_id)+'/files/'+encodeURIComponent(plan.targets[0].file_id));if(cloud115TransferSelection!==selection)return;if(!r.file||r.file.id!==plan.targets[0].file_id)throw new Error('目标文件 ID 无法核对');
    $('cloud115Account').value=plan.account_id;cloud115ResetBrowser();cloud115View.account=plan.account_id;cloud115View.parent=r.file.parent_id||'0';cloud115View.items=[r.file];if(mode==='organize')cloud115OrganizeChoose(0);else {cloud115Choose(0);$('cloud115Sidecars').value=plan.targets.slice(1).map(function(t){return t.file_id;}).join(',');}
  }catch(e){if(cloud115TransferSelection===selection)$('cloud115TransferResult').textContent=e.message;}
}
