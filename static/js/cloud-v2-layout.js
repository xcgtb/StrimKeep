/* StrimKeep My Drive V2: presentation only. Existing API handlers and IDs are retained. */
(function () {
  'use strict';
  if (!window.cloudWorkbench || typeof window.cloudWorkbench.load !== 'function') return;
  var originalLoad = window.cloudWorkbench.load;
  var activePage = 'home';
  var transferMode = 'transfer';
  var organizeMode = 'run';
  var mounted = false;
  var refreshSerial = 0;
  var titles = {
    accounts: ['115 账号','扫码、Cookie 与维护'],
    files: ['文件管理','浏览云端目录'],
    transfer: ['资源接收','115 转存、免转存、离线下载'],
    organize: ['影视整理','扫描、识别、归档与版本策略'],
    strm: ['STRM 媒体库','同步与播放引用'],
    playback: ['Emby 与播放','独立连接和 302 网关'],
    channels: ['频道监控','规则、筛选与消息'],
    inbox: ['接收历史','频道和 TG 接收结果'],
    jobs: ['任务中心','执行进度与异常核实']
  };

  function id(value) { return document.getElementById(value); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function button(label, action, cls) {
    var n = el('button', cls || 'cw-v2-action', label);
    n.type = 'button'; n.dataset.v2Action = action; return n;
  }
  function store(key, value) { try { sessionStorage.setItem('sk-drive-v2-'+key,value); } catch (_) {} }
  function recall(key, fallback) { try { return sessionStorage.getItem('sk-drive-v2-'+key) || fallback; } catch (_) { return fallback; } }
  function safeText(n, text) { if (n) n.textContent = String(text); }
  function section(title, description, modifier) {
    var n = el('div', 'cw-v2-panel'+(modifier?' '+modifier:''));
    var h = el('div','cw-v2-panel-heading');
    h.append(el('h3','',title),el('p','',description));n.append(h);return n;
  }
  function openPage(name) {
    var trigger=id('cw-tabs') && id('cw-tabs').querySelector('[data-cw-tab="'+name+'"]');
    if (trigger) trigger.click();
  }
  function homeView() {
    var home = el('section', 'cw-v2-home');home.id='cw-v2-home'; home.hidden=true;
    var lead=section('从这里开始','主流程与实时任务分开，日常使用不必翻遍所有设置。','cw-v2-home-lead');
    var actions=el('div','cw-v2-home-actions');
    [['添加资源','page:transfer'],['立即整理','page:organize'],['生成 STRM','page:strm']].forEach(function(pair,i){actions.append(button(pair[0],pair[1],i===0?'cw-v2-primary':'cw-v2-action'));});
    lead.append(actions);home.append(lead);
    var cards=el('div','cw-v2-summary');
    [['已登录账号','cw-v2-account-count','查看账号配置'],['待处理任务','cw-v2-pending-count','进行中 / 待核实'],['最近任务','cw-v2-recent-name','查看任务中心']].forEach(function(row){
      var c=el('div','cw-v2-metric');c.append(el('span','',row[0]));var strong=el('strong','', '—');strong.id=row[1];c.append(strong,el('small','',row[2]));cards.append(c);
    });home.append(cards);
    var flows=section('常用工作流','三种资源入口，共用同一套识别、STRM 和播放能力。');
    var grid=el('div','cw-v2-flow-grid');
    [
      {name:'115 分享转存',detail:'选择文件 → 转存 → 识别整理 → 自有 STRM',action:'page:transfer'},
      {name:'分享免转存',detail:'选择分享文件 → TMDB 识别 → 分享 STRM',action:'mode:share'},
      {name:'ED2K / 磁力',detail:'提交离线任务 → 核实完成 → 整理归档',action:'mode:offline'},
      {name:'检查历史与异常',detail:'查看进度、待核实写入与失败原因',action:'page:jobs'}
    ].forEach(function(item){var card=el('button','cw-v2-flow');card.type='button';card.dataset.v2Action=item.action;card.append(el('strong','',item.name),el('span','',item.detail),el('em','','查看 →'));grid.append(card);});
    flows.append(grid);home.append(flows);
    var caution=el('p','cw-v2-footnote','提示：镜像运行后的真实进度以任务中心为准；云下载提交和 STRM 生成不等于 Emby 已完成入库或实播。');home.append(caution);
    return home;
  }
  async function homeMetrics() {
    var serial=++refreshSerial;
    var accounts=id('cw-account');safeText(id('cw-v2-account-count'),accounts?accounts.options.length:'—');
    try {
      var response=await fetch('/api/cloud/jobs',{credentials:'same-origin',cache:'no-store'});
      if (!response.ok) throw Error('任务接口不可用');
      var result=await response.json();
      if (serial!==refreshSerial) return;
      var jobs=Array.isArray(result.items)?result.items:[];
      var pending=jobs.filter(function(j){return ['queued','running','uncertain','needs_review'].includes(j.state);}).length;
      safeText(id('cw-v2-pending-count'), pending);
      safeText(id('cw-v2-recent-name'),jobs.length?(jobs[0].message||'已有任务'):'暂无任务');
    } catch (_) {
      if (serial===refreshSerial) {safeText(id('cw-v2-pending-count'),'暂不可用');safeText(id('cw-v2-recent-name'),'请打开任务中心');}
    }
  }
  function setActive(name) {
    activePage=name;store('page',name);
    var home=id('cw-v2-home');if(home)home.hidden=name!=='home';
    var main=id('tab-cloud'), nav=id('cw-tabs');
    if (name==='home') {
      var content=main && main.querySelector('.cw-content');
      if(content) Array.from(content.children).forEach(function(child){if(child.id && /^cw-(accounts|files|transfer|organize|strm|playback|channels|inbox|jobs)$/.test(child.id)) child.hidden=true;});
      homeMetrics();
    }
    if(nav)nav.querySelectorAll('button').forEach(function(b){var isHome=b.dataset.v2Action==='home';var on=isHome?name==='home':name!=='home'&&b.dataset.cwTab===name;b.classList.toggle('on',on);b.setAttribute('aria-current',on?'page':'false');});
    var crumb=id('cw-v2-current');safeText(crumb,name==='home'?'工作概览':titles[name]?titles[name][0]:'我的网盘');
  }

  function enhanceTransfer() {
    var host=id('cw-transfer');if(!host||id('cw-v2-transfer-modes'))return;
    host.classList.add('cw-v2-structured');
    var original=Array.from(host.children);
    var head=el('div','cw-v2-page-head');head.append(el('h2','','资源接收'),el('p','','按资源来源选择操作方式，先预览再确认。'));host.prepend(head);
    var tabs=el('nav','cw-v2-mode-tabs');tabs.id='cw-v2-transfer-modes';tabs.setAttribute('aria-label','资源接收模式');
    [['transfer','115 分享转存'],['share','分享免转存'],['offline','磁力 / ED2K / 秒传']].forEach(function(pair){var b=button(pair[1],'transfer:'+pair[0],'cw-v2-mode');b.dataset.v2Mode=pair[0];tabs.append(b);});
    host.append(tabs);
    var common=section('分享资源','填写分享链接、预览目录并勾选要处理的文件。');common.id='cw-v2-transfer-common';
    var normal=section('转存到我的 115','保存到自己网盘，可选择联动整理与自有 STRM。');normal.id='cw-v2-transfer-normal';
    var share=section('生成分享 STRM','直接引用分享文件，不转存、不修改对方网盘资源。');share.id='cw-v2-transfer-share';
    var offline=section('离线下载与机器人接收','云下载提交成功不等于资源已下载完成。');offline.id='cw-v2-transfer-offline';
    var phase='common';
    original.forEach(function(n){
      if (n.tagName==='H3' && n.textContent.includes('资源接收')){n.hidden=true;host.append(n);return;}
      if (n.id==='cw-svc-receiving'){offline.append(n);return;}
      if (n.tagName==='H4'&&n.textContent.includes('分享免转存')) phase='share';
      var action=n.getAttribute('data-cwa');
      if ((n.tagName==='LABEL' && n.querySelector('#cw-auto-organize')) || action==='receive') {normal.append(n);return;}
      if (n.hasAttribute('data-directory-role')&&n.dataset.directoryRole==='transfer'||n.id==='cw-transfer-directory'){normal.append(n);return;}
      (phase==='share'?share:common).append(n);
    });
    host.append(common,normal,share,offline);
    transferMode=recall('transfer','transfer');updateTransfer();
  }
  function updateTransfer(){
    ['transfer','share','offline'].forEach(function(key){var n=id('cw-v2-transfer-'+(key==='transfer'?'normal':key));if(n)n.hidden=key!==transferMode;});
    var common=id('cw-v2-transfer-common');if(common)common.hidden=transferMode==='offline';
    var tabs=id('cw-v2-transfer-modes');if(tabs)tabs.querySelectorAll('button').forEach(function(b){var on=b.dataset.v2Mode===transferMode;b.classList.toggle('active',on);b.setAttribute('aria-pressed',on?'true':'false');});
  }

  function enhanceOrganize() {
    var host=id('cw-organize');if(!host||id('cw-v2-organize-tabs'))return;
    host.classList.add('cw-v2-structured');
    var original=Array.from(host.children);
    var title=el('div','cw-v2-page-head');title.append(el('h2','','影视整理'),el('p','','先执行扫描与预览；需要调整规则时，按模块进入设置。'));host.prepend(title);
    var tabs=el('nav','cw-v2-mode-tabs cw-v2-organize-tabs');tabs.id='cw-v2-organize-tabs';tabs.setAttribute('aria-label','影视整理工作区');
    var specs=[['run','开始整理'],['naming','识别与命名'],['category','分类归档'],['quality','八维洗版'],['schedule','定时任务'],['advanced','过滤与高级']];
    var panes={};
    specs.forEach(function(pair){var b=button(pair[1],'organize:'+pair[0],'cw-v2-mode');b.dataset.v2Mode=pair[0];tabs.append(b);var pane=section(pair[1],{
      run:'选择目录、确认扫描结果，再执行归档和 STRM 联动。',naming:'TMDB 识别、命名模板、FFprobe 和人工修正。',category:'按作品类型、地区和年份创建可复用目录。',quality:'八维策略默认先预览，删除旧源前请进行真实账号验收。',schedule:'自动整理来源和 Cron 规则统一管理。',advanced:'文件过滤、未识别目录、清理与延时设置。'
    }[pair[0]]);pane.classList.add('cw-v2-organize-pane');pane.id='cw-v2-organize-'+pair[0];panes[pair[0]]=pane;});
    host.append(tabs);
    var state='run';
    original.forEach(function(n){
      var heading=n.tagName==='SUMMARY'?n.textContent:(n.tagName==='DETAILS'&&n.querySelector('summary')?n.querySelector('summary').textContent:'');
      if(n.classList.contains('cw-organize-toolbar')) {panes.naming.append(n);return;}
      if(n.tagName==='H3'&&n.textContent.includes('影视整理')){n.hidden=true;host.append(n);return;}
      if(n.tagName==='H4'&&n.textContent.includes('修正作品识别'))state='naming';
      if(n.tagName==='H4'&&n.textContent.includes('自动整理任务'))state='schedule';
      if(heading.includes('文件过滤')){panes.advanced.append(n);return;}
      if(heading.includes('识别与自定义命名')){n.open=true;panes.naming.append(n);return;}
      if(heading.includes('分类策略')){n.open=true;panes.category.append(n);return;}
      if(heading.includes('整理八维洗版策略')){n.open=true;panes.quality.append(n);return;}
      if(n.id==='cw-link-intake'){panes.schedule.append(n);return;}
      var save=n.getAttribute('data-cwa');
      if(save==='saveNaming'){panes.naming.append(n);return;}
      if(n.querySelector&&n.querySelector('#cw-unknown-enabled')){panes.advanced.append(n);return;}
      if(n.querySelector&&n.querySelector('#cw-cleanup-empty')){panes.advanced.append(n);return;}
      // Scan and apply always stay together. Existing actions remain untouched.
      if(['plan','apply'].includes(save)||n.id==='cw-plan-list'||(n.tagName==='LABEL'&&n.querySelector('#cw-generate'))){panes.run.append(n);return;}
      panes[state].append(n);
    });
    var runTip=el('div','cw-v2-run-tip');runTip.append(el('strong','', '配置与操作分开'),el('span','', '初次使用请先在「识别与命名」「分类归档」中调整设置并保存；日常整理可直接扫描预览。'),button('保存当前整理配置','save-naming','cw-v2-action'));panes.run.insertBefore(runTip,panes.run.children[1]||null);
    specs.forEach(function(pair){host.append(panes[pair[0]]);});
    organizeMode=recall('organize','run');updateOrganize();
  }
  function updateOrganize() {
    ['run','naming','category','quality','schedule','advanced'].forEach(function(key){var n=id('cw-v2-organize-'+key);if(n)n.hidden=key!==organizeMode;});
    var tabs=id('cw-v2-organize-tabs');if(tabs)tabs.querySelectorAll('button').forEach(function(b){var on=b.dataset.v2Mode===organizeMode;b.classList.toggle('active',on);b.setAttribute('aria-pressed',on?'true':'false');});
  }
  function enhanceNav(root) {
    var nav=id('cw-tabs');if(!nav)return;
    var homeBtn=button('工作概览','home','cw-v2-menu-home');nav.prepend(homeBtn);
    nav.querySelectorAll('[data-cw-tab]').forEach(function(btn){var key=btn.dataset.cwTab;if(titles[key]){btn.textContent=titles[key][0];btn.title=titles[key][1];}});
    var heading=nav.parentElement && nav.parentElement.querySelector('h3');if(heading)heading.textContent='我的网盘';
    var intro=root.querySelector('.page-intro');if(intro)intro.hidden=true;
    var header=el('header','cw-v2-hero');header.append(el('div','cw-v2-kicker','MY DRIVE  /  STRIMKEEP'));
    header.append(el('h2','','我的网盘'));
    var bar=el('div','cw-v2-hero-bottom');bar.append(el('p','','115 资源接收、云端整理、STRM 与 Emby 统一管理。'));
    var current=el('span','cw-v2-crumb','工作概览');current.id='cw-v2-current';bar.append(current);header.append(bar);
    var account=id('cw-account'), accountBar=account&&account.closest('.panel');if(accountBar){accountBar.classList.add('cw-v2-accountbar');accountBar.parentNode.insertBefore(header,accountBar);}else root.prepend(header);
    var home=homeView(),content=root.querySelector('.cw-content');if(content)content.append(home);
    root.classList.add('cw-v2-active');
    nav.addEventListener('click',function(event){var target=event.target.closest('button');if(!target)return;
      if(target.dataset.v2Action==='home'){setActive('home');return;}
      if(target.dataset.cwTab){var name=target.dataset.cwTab;refreshSerial++;setActive(name);}
    });
    root.addEventListener('click',function(event){var target=event.target.closest('[data-v2-action]');if(!target || !root.contains(target))return;
      var action=target.dataset.v2Action;
      if(action==='home')return;
      if(action==='save-naming'){var save=id('cw-organize').querySelector('[data-cwa="saveNaming"]');if(save)save.click();return;}
      if(action.startsWith('page:')){var page=action.slice(5);openPage(page);return;}
      if(action.startsWith('mode:')){transferMode=action.slice(5);store('transfer',transferMode);updateTransfer();openPage('transfer');return;}
      if(action.startsWith('transfer:')){transferMode=action.slice(9);store('transfer',transferMode);updateTransfer();return;}
      if(action.startsWith('organize:')){organizeMode=action.slice(9);store('organize',organizeMode);updateOrganize();return;}
    });
    if(account)account.addEventListener('change',function(){refreshSerial++;if(activePage==='home')homeMetrics();});
  }
  function enhance() {
    if(mounted)return;
    var root=id('tab-cloud');if(!root||!id('cw-tabs')||!id('cw-organize')||!id('cw-transfer'))return;
    mounted=true;
    enhanceTransfer();enhanceOrganize();enhanceNav(root);
    var preferred=recall('page','home');
    if(preferred==='home')setActive('home');else if(titles[preferred])openPage(preferred);else setActive('home');
  }
  window.cloudWorkbench.load=async function(){
    var result=await originalLoad.apply(this,arguments);
    enhance();
    if(mounted && activePage==='home')setActive('home');
    return result;
  };
  // No network writes or changes to provider/governance behavior; only DOM relocation.
  window.cloudWorkbenchV2={enhance:enhance,openPage:openPage};
})();
