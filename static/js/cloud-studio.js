/* Daily workspaces and one configuration directory; all existing action IDs survive. */
(function(){
  'use strict';
  if(!window.cloudWorkbench)return;
  var originalLoad=cloudWorkbench.load,mounted=false,request=0;
  var titles={workspace:'网盘工作台',configuration:'网盘设置',accounts:'账号与授权',files:'网盘文件',transfer:'资源接收','share-library':'分享媒体',organize:'影视整理',strm:'STRM 媒体库',playback:'Emby 与播放',channels:'频道自动接收',inbox:'接收记录',jobs:'任务中心'};
  var descriptions={workspace:'接收、整理、生成与入库，在一个工作区里掌握。',configuration:'按处理环节配置一次，日常操作回到工作台。',transfer:'选择接收位置，提交资源，按需接续整理。',files:'浏览真实目录与文件，按需展开，不预加载整个网盘。',organize:'先保存规则，再预览或执行。已完成与待处理分别记录。',strm:'播放引用、生成结果与 Emby 实际入库分开查看。',playback:'入库连接、播放反代与机器人通知各自保存，互不覆盖。',jobs:'未知转存先核实；进度、异常与结果在这里查看。'};
  var pages=[['workspace','工作台','grid'],['transfer','资源接收','download'],['files','网盘文件','folder'],['strm','媒体库','film'],['jobs','任务中心','list'],['configuration','设置','settings']];
  var setup=[['01','账号与授权','扫码登录、设备与源文件删除权限','accounts','login','folder'],['02','来源与归档','来源、归档与未识别文件的去向','organize','organize-directories','folder'],['03','识别与命名','父目录身份、媒体信息与命名模板','organize','naming','film'],['04','分类与版本','分类目录与版本比较优先级','organize','classification','grid'],['05','自动整理','执行时间、接收目录与空目录收尾','organize','automation','clock'],['06','STRM 输出','NAS 输出位置、播放入口和元数据','strm','strm-output','link'],['07','Emby 入库','连接、路径映射与实际入库确认','playback','emby','film'],['08','播放与反代','可选监听端口，与上游反代分开','playback','playback','link'],['09','机器人通知','总开关、分模块通知与入库海报','playback','notifications','bell'],['10','频道与接收','频道规则、历史读取与接收记录','channels','channel','download']];
  var paths={grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',download:'M12 3v12m-5-5 5 5 5-5M4 15v5h16v-5',folder:'M3 6h7l2 3h9v11H3z',film:'M3 4h18v16H3z M7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4',list:'M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',clock:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3 2',link:'M9 15l6-6M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 10a4 4 0 0 0 6 0l4-4a4 4 0 0 0-6-6l-1 1',bell:'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5zM10 21h4'};
  function svg(key){return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="'+paths[key]+'"/></svg>';}
  function id(key){return document.getElementById(key);}
  function safe(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function element(tag,cls,html){var n=document.createElement(tag);n.className=cls;if(html)n.innerHTML=html;return n;}
  function route(page,module){cloudWorkbench.navigate(page);if(module&&window.cloudPanels)cloudPanels.open(page,module);}
  function activePage(name){return pages.some(function(p){return p[0]===name;})?name:name==='organize'?'workspace':name==='share-library'?'strm':'configuration';}
  function pageChanged(name){
    try{sessionStorage.setItem('sk-drive-page',name);}catch(_){}
    if(!mounted)return;
    var active=activePage(name);
    document.querySelectorAll('[data-studio-nav]').forEach(function(n){var on=n.dataset.studioNav===active;n.classList.toggle('on',on);n.setAttribute('aria-current',on?'page':'false');});
    document.querySelectorAll('[data-drive-page]').forEach(function(n){var on=n.dataset.drivePage===active;n.classList.toggle('active',on);n.setAttribute('aria-current',on?'page':'false');});
    id('cw-page-title').textContent=titles[name]||'我的网盘';id('cw-studio-description').textContent=descriptions[name]||'配置在当前模块保存，不会自动执行远端操作。';
    if(window.__activeTab==='cloud'&&id('tabTitle'))id('tabTitle').textContent='我的网盘';
    if(name==='workspace')refresh();
  }
  function hub(){
    var parent=id('cw-configuration');parent.innerHTML='<div class="cw-studio-section-heading"><div><span class="cw-studio-eyebrow">配置中心</span><h3>让每一步都有明确的去向</h3><p>按处理顺序排列，修改在对应页面保存。</p></div><label class="cw-studio-filter"><span>查找设置</span><input type="search" id="cw-studio-filter" placeholder="搜索命名、Emby、通知…"></label></div><div class="cw-studio-setup-grid">'+setup.map(function(r){return '<button type="button" class="cw-studio-setup" data-studio-page="'+r[3]+'" data-studio-module="'+r[4]+'"><span class="cw-studio-setup-icon">'+svg(r[5])+'</span><span><small>'+r[0]+'</small><strong>'+r[1]+'</strong><p>'+r[2]+'</p></span><span class="cw-studio-arrow">↗</span></button>';}).join('')+'</div><p id="cw-studio-filter-empty" class="cw-studio-empty" hidden>没有匹配项，试试“目录”“入库”或“命名”。</p>';
    id('cw-studio-filter').oninput=function(){var q=this.value.trim().toLowerCase(),count=0;parent.querySelectorAll('.cw-studio-setup').forEach(function(n){n.hidden=!n.textContent.toLowerCase().includes(q);if(!n.hidden)count++;});id('cw-studio-filter-empty').hidden=!!count;};
  }
  function workspace(){
    var parent=id('cw-workspace');parent.innerHTML='<div class="cw-studio-welcome"><div><span class="cw-studio-eyebrow">日常工作 / 一站处理</span><h3>从一份资源，<br>到你的媒体库。</h3><p>把操作和配置分开，让接收、整理与入库更有条理。</p><div class="cw-studio-welcome-actions"><button type="button" class="btn" data-studio-page="transfer">'+svg('download')+' 接收资源</button><button type="button" class="btn gray" data-studio-page="organize" data-studio-module="run">开始整理 ↗</button></div></div><div class="cw-studio-flow"><span><i>01</i>接收资源</span><b>→</b><span><i>02</i>整理归档</span><b>→</b><span><i>03</i>生成 STRM</span><b>→</b><span><i>04</i>确认入库</span><small>每一步分别记录结果，不把“已提交”当作“已完成”。</small></div></div><div class="cw-studio-metrics">'+[['running','进行中的任务','最近 50 条任务'],['attention','需要处理','异常与待核实任务'],['assets','最近生成引用','当前账号 · 最多 100 条'],['confirmed','入库已确认','最近 100 条检查记录']].map(function(r){return '<div><span>'+r[1]+'</span><strong id="cw-studio-'+r[0]+'">—</strong><small>'+r[2]+'</small></div>';}).join('')+'</div><div class="cw-studio-work-grid"><div><article class="cw-studio-card"><div class="cw-studio-card-heading"><div><h3>最近任务</h3><p>所有账号的最近处理记录</p></div><button type="button" class="cw-studio-text-button" data-studio-page="jobs">查看全部 ↗</button></div><div id="cw-studio-recent" aria-live="polite"></div></article><article class="cw-studio-card cw-studio-shortcuts"><h3>继续你的工作</h3><div>'+[['files','网盘文件','查看来源与归档','folder'],['strm','STRM 媒体库','扫描生成与播放引用','film'],['inbox','接收记录','频道与机器人任务','list'],['configuration','网盘设置','所有配置集中查找','settings']].map(function(r){return '<button type="button" data-studio-page="'+r[0]+'">'+svg(r[3])+'<span><strong>'+r[1]+'</strong><small>'+r[2]+'</small></span><b>↗</b></button>';}).join('')+'</div></article></div><aside id="cw-studio-inspector"></aside></div><p id="cw-studio-status" role="status" class="hint"></p>';
    var pipeline=document.querySelector('.cw-pipeline');if(pipeline){pipeline.hidden=false;delete pipeline.dataset.cwModule;id('cw-studio-inspector').append(pipeline);pipeline.querySelector('h3').textContent='处理链路检查';}
  }
  async function api(path){var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},15000);try{var response=await fetch('/api/cloud'+path,{credentials:'same-origin',cache:'no-store',signal:controller.signal}),data=await response.json();if(!response.ok||!Array.isArray(data.items))throw Error('状态读取失败，请刷新重试');return data;}finally{clearTimeout(timer);}}
  async function refresh(){
    if(!mounted)return;var token=++request,account=id('cw-account').value;
    id('cw-studio-recent').textContent='正在读取最近任务…';
    try{var results=await Promise.allSettled([api('/jobs'),api('/assets?limit=100&offset=0'),account?api('/accounts/'+encodeURIComponent(account)+'/ingestion'):Promise.resolve({items:[]})]);
      if(token!==request||account!==id('cw-account').value)return;
      var values=results.map(function(r){return r.status==='fulfilled'?r.value.items||[]:null;}),jobs=values[0],assets=values[1],receipts=values[2];
      id('cw-studio-running').textContent=jobs?jobs.filter(function(j){return ['running','queued'].includes(j.state);}).length:'—';id('cw-studio-attention').textContent=jobs?jobs.filter(function(j){return ['failed','uncertain','needs_review'].includes(j.state);}).length:'—';
      id('cw-studio-assets').textContent=assets?assets.filter(function(a){return a.account_id===account&&a.state==='active';}).length:'—';id('cw-studio-confirmed').textContent=receipts?receipts.filter(function(r){return r.state==='confirmed';}).length:'—';
      var states={queued:'等待执行',running:'执行中',success:'已完成',failed:'失败',uncertain:'待核实',needs_review:'需要处理',cancelled:'已取消',stopped:'已停止'},kinds={transfer:'分享转存',organize:'影视整理',workflow:'自动整理',sync:'STRM 生成',publish:'延时生成',channel:'频道接收',intake:'资源接收',share_strm:'分享媒体',maintenance:'日常维护',categories:'创建分类'};
      id('cw-studio-recent').innerHTML=jobs===null?'<p class="cw-studio-empty">任务读取失败，请刷新重试。</p>':jobs.length?jobs.slice(0,5).map(function(j){return '<button type="button" class="cw-studio-task" data-studio-job="'+safe(j.id)+'"><span class="cw-studio-task-icon">'+svg(j.state==='running'?'clock':'film')+'</span><span><strong>'+safe(kinds[j.kind]||j.kind)+'</strong><small>'+safe(j.progress&&j.progress.current||j.message||'等待执行')+'</small></span><span class="cw-studio-state" data-state="'+safe(j.state)+'">'+safe(states[j.state]||j.state)+'</span><b>↗</b></button>';}).join(''):'<div class="cw-studio-empty"><strong>还没有处理任务</strong><p>从接收资源开始，或整理已有来源目录。</p><button type="button" class="btn gray" data-studio-page="transfer">接收第一份资源</button></div>';
      id('cw-studio-status').textContent=results.some(function(r){return r.status==='rejected';})?'部分状态未读取成功，未知项显示为 —，没有当作 0。':'刚刚更新 · 只读取任务记录，不扫描整个网盘。';
    }catch(error){if(token===request)id('cw-studio-status').textContent=error.message;}
    if(window.cloudPanels)cloudPanels.refresh();
  }
  function mount(){
    if(mounted)return;var root=id('tab-cloud');if(!id('cw-workspace'))return;mounted=true;root.classList.add('cw-studio');
    var old=id('cw-page-title');if(old)old.remove();
    var heading=element('div','cw-studio-heading','<div><span class="cw-studio-eyebrow">STRIMKEEP / 我的网盘</span><h2 id="cw-page-title">网盘工作台</h2><p id="cw-studio-description"></p></div><div class="cw-studio-account"></div>');heading.querySelector('.cw-studio-account').append(root.querySelector('.cw-accountbar'));var refreshButton=element('button','cw-studio-refresh',svg('clock')+' 刷新状态');refreshButton.type='button';refreshButton.onclick=refresh;heading.querySelector('.cw-studio-account').append(refreshButton);root.prepend(heading);
    var nav=element('nav','cw-studio-nav',pages.map(function(p){return '<button type="button" data-studio-page="'+p[0]+'" data-studio-nav="'+p[0]+'">'+svg(p[2])+'<span>'+p[1]+'</span></button>';}).join(''));nav.setAttribute('aria-label','我的网盘工作区');heading.after(nav);
    root.addEventListener('click',function(event){var n=event.target.closest('[data-studio-page]');if(n){route(n.dataset.studioPage,n.dataset.studioModule);return;}var job=event.target.closest('[data-studio-job]');if(job)cloudWorkbench.inspectJob(job.dataset.studioJob);});hub();workspace();
  }
  function openPage(name){if(titles[name])return cloudWorkbench.navigate(name);}
  cloudWorkbench.load=async function(){var preferred='workspace';try{var old=sessionStorage.getItem('sk-drive-page');if(titles[old])preferred=old;}catch(_){}var result=await originalLoad.apply(this,arguments);mount();if(result!==false)openPage(preferred);return result;};
  window.cloudWorkbenchV2={enhance:mount,openPage:openPage,pageChanged:pageChanged,refresh:refresh};
})();
