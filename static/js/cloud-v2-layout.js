/* v1.3.7 continuation: main sidebar navigation and explicit module saves. */
(function(){
  'use strict';
  if(!window.cloudWorkbench)return;
  var originalLoad=window.cloudWorkbench.load,mounted=false;
  var titles={accounts:'115网盘配置',files:'文件管理',transfer:'链接转存与机器人', 'share-library':'分享 STRM',organize:'影视整理',strm:'STRM生成',playback:'Emby与播放',channels:'115频道监控',inbox:'监控与接收历史',jobs:'整理历史与任务'};
  function id(n){return document.getElementById(n);}
  function remember(name){try{sessionStorage.setItem('sk-drive-page',name);}catch(_){}}
  function recalled(){try{return sessionStorage.getItem('sk-drive-page')||'accounts';}catch(_){return 'accounts';}}
  function pageChanged(name){
    remember(name);var menu=document.querySelector('.nav-drive-children'),toggle=document.querySelector('.nav-drive-toggle');if(menu)menu.hidden=false;if(toggle)toggle.setAttribute('aria-expanded','true');document.querySelectorAll('[data-drive-page]').forEach(function(n){var on=n.dataset.drivePage===name;n.classList.toggle('active',on);n.setAttribute('aria-current',on?'page':'false');});
    if(id('cw-page-title'))id('cw-page-title').textContent='115网盘 · '+(titles[name]||'我的网盘');
    if(window.__activeTab==='cloud'&&id('tabTitle'))id('tabTitle').textContent=titles[name]||'我的网盘';
  }
  function enhance(){
    if(mounted)return;var root=id('tab-cloud');if(!root||!id('cw-tabs'))return;mounted=true;
    root.classList.add('cw-v2-active');root.querySelectorAll('.cw-content > section > h3').forEach(function(n){n.hidden=true;});
    // The application sidebar owns navigation; keep only existing event hooks in hidden DOM.
    var localNav=id('cw-tabs').closest('aside');if(localNav)localNav.hidden=true;
    var intro=root.querySelector('.page-intro');if(intro)intro.remove();
    var heading=document.createElement('h2');heading.id='cw-page-title';root.prepend(heading);
    root.querySelectorAll('.cw-config-toolbar').forEach(function(bar){
      var section=bar.closest('section'),save=document.createElement('button');save.type='button';save.className='btn cw-config-save';save.textContent='保存修改';
      var commands={channels:'saveChannel',transfer:'saveReceiving'};
      if(section.id==='cw-organize'){
        var original=bar.querySelector('[data-link-action="saveOrganize"]');if(original)original.textContent='保存修改';
      }else if(commands[section.id.replace('cw-','')]){
        var command=commands[section.id.replace('cw-','')];
        save.onclick=function(){var selector='[data-svc="'+command+'"]',target=Array.from(section.querySelectorAll(selector)).find(function(n){return n!==save;});if(target)target.click();};bar.append(save);
      }
    });
  }
  function openPage(name){if(!titles[name])return;remember(name);return window.cloudWorkbench.navigate(name);}
  window.cloudWorkbench.load=async function(){var preferred=recalled(),result=await originalLoad.apply(this,arguments);enhance();if(result!==false)openPage(preferred);return result;};
  window.cloudWorkbenchV2={enhance:enhance,openPage:openPage,pageChanged:pageChanged};
})();
