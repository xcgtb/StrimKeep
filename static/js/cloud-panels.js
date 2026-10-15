/* Progressive layout: move existing live controls, never clone or rebuild their handlers. */
window.cloudPanels=(function(){
  'use strict';
  var cards=[];
  function el(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
  function remember(key,open){try{localStorage.setItem('cw-panel:'+key,open?'1':'0');}catch(error){}}
  function restored(key,fallback){try{var value=localStorage.getItem('cw-panel:'+key);return value===null?fallback:value==='1';}catch(error){return fallback;}}
  function summary(title,description){var s=el('summary','cw-config-summary'),copy=el('span','cw-config-copy');copy.append(el('strong','',title),el('span','hint',description));s.append(el('span','cw-config-symbol','▦'),copy,el('span','cw-config-toggle'));return s;}
  function enhance(detail,key,title,description,open){
    if(detail.dataset.configKey)return detail;
    var old=detail.querySelector('summary'),body=el('div','cw-config-body');
    Array.from(detail.childNodes).forEach(function(n){if(n!==old)body.append(n);});if(old)old.remove();
    detail.classList.add('cw-config-card');detail.classList.remove('cw-settings-group');detail.dataset.configKey=key;
    detail.append(summary(title,description),body);detail.open=restored(key,!!open);
    detail.addEventListener('toggle',function(){remember(key,detail.open);});cards.push(detail);return detail;
  }
  function wrap(first,last,key,title,description,open){
    if(!first||!last||first.parentNode!==last.parentNode)return;
    var parent=first.parentNode,detail=el('details'),cursor=first;
    // Freeze the sibling range before inserting the new parent.
    var nodes=[];while(cursor){nodes.push(cursor);if(cursor===last)break;cursor=cursor.nextSibling;}
    if(nodes[nodes.length-1]!==last)return;
    parent.insertBefore(detail,first);nodes.forEach(function(n){detail.append(n);});return enhance(detail,key,title,description,open);
  }
  function top(node,parent){if(!node)return null;while(node.parentNode&&node.parentNode!==parent)node=node.parentNode;return node.parentNode===parent?node:null;}
  function byId(id){return document.getElementById(id);}
  function existing(id,key,title,description){var n=byId(id);if(n)wrap(n,n,key,title,description,false);}
  function reveal(node){for(var p=node;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;node.scrollIntoView({behavior:'smooth',block:'center'});if(node.focus)node.focus({preventScroll:true});}
  function openLogs(){
    if(typeof switchTab==='function')switchTab('history');
    var source=byId('logSource'),query=byId('logQuery');
    if(source&&query){source.value='cloud';query.value='';if(typeof filterRuntimeLogs==='function')filterRuntimeLogs();}
  }
  function toolbar(section){
    if(!section||!section.querySelector('.cw-config-card'))return;
    var bar=el('div','cw-config-toolbar'),label=el('label','cw-config-search'),input=el('input');input.type='search';input.placeholder='搜索并定位配置项';input.setAttribute('aria-label','搜索当前页面配置');label.append(input);
    var search=el('button','btn gray','定位'),expand=el('button','btn gray','展开全部'),collapse=el('button','btn gray','收起全部'),logs=el('button','btn gray','查看实时日志');[search,expand,collapse,logs].forEach(function(b){b.type='button';});
    var status=el('span','hint');status.setAttribute('role','status');
    function find(){var value=input.value.trim().toLowerCase();if(!value){status.textContent='输入目录、命名、媒体信息等关键词。';return;}var found=Array.from(section.querySelectorAll('.cw-config-card')).find(function(c){return c.textContent.toLowerCase().includes(value);});if(found){reveal(found);status.textContent='已展开匹配设置。';}else status.textContent='本页没有匹配的设置。';}
    search.onclick=find;input.onkeydown=function(event){if(event.key==='Enter'){event.preventDefault();find();}};
    expand.onclick=function(){section.querySelectorAll('.cw-config-card').forEach(function(c){c.open=true;});};collapse.onclick=function(){section.querySelectorAll('.cw-config-card').forEach(function(c){c.open=false;});};
    logs.onclick=openLogs;
    var more=el('details','cw-toolbar-more'),moreTitle=el('summary','','更多'),menu=el('div');menu.append(expand,collapse,logs);more.append(moreTitle,menu);bar.append(label,search,more);
    var original=section.querySelector('.cw-organize-toolbar');
    if(original){original.querySelectorAll('label').forEach(function(n){n.hidden=true;});bar.append(original);}
    bar.append(status);section.insertBefore(bar,section.children[1]||null);
    section.querySelectorAll('.cw-config-body').forEach(function(body){body.querySelectorAll('button').forEach(function(b){if(/^保存|检测并保存/.test(b.textContent))b.classList.add('cw-config-save');});});
  }
  function mount(){
    cards=[];
    var accounts=byId('cw-accounts'),transfer=byId('cw-transfer'),organize=byId('cw-organize'),strm=byId('cw-strm'),playback=byId('cw-playback'),channels=byId('cw-channels');
    if(accounts){var end=byId('cw-svc-maintenance');wrap(accounts.children[1],end?end.previousSibling:accounts.lastChild,'login','115 登录凭证','选择客户端，扫码或填写 Cookie；在本卡片内管理当前账号授权。',false);}
    existing('cw-svc-maintenance','maintenance','115 日常维护','配置签到、回收站保留天数与执行时间。');
    if(transfer)wrap(transfer.children[1],byId('cw-transfer-status'),'receive','115链接转存','保存转存目录后直接提交链接，后续由定时整理扫描。',true);
    var share=byId('cw-share-library');if(share)wrap(share.children[1],share.lastChild,'share-library','分享免转存','读取分享目录，选中资源后生成分享STRM。',true);
    existing('cw-svc-receiving','intake','磁力、ed2k 与机器人接收','分别选择保存目录，配置机器人接收与查询云下载状态。');
    if(organize){var grid=top(byId('cw-source-cid'),organize),first=organize.querySelector('details');if(grid&&first)wrap(grid,first.previousSibling,'organize-directories','扫描来源与归档目录','直接读取115目录并保存；后续整理复用相同目录。',true);
      Array.from(organize.querySelectorAll('details.cw-settings-group')).forEach(function(d,i){var title=d.querySelector('summary').textContent;enhance(d,'organize-'+i,title,{'文件过滤':'按类型、大小、关键词与正则排除文件。','识别与自定义命名':'TMDB 识别、ffprobe 媒体信息和目录/文件命名模板。','分类策略':'类型、地区与年份分类，按需创建并复用目录。','整理八维洗版策略':'配置比较优先级、预览或执行模式和杜比分组共存。'}[title]||'展开配置具体规则。',false);});}
    if(organize){
      var unknown=top(byId('cw-unknown-enabled'),organize),cleanup=top(byId('cw-cleanup-empty'),organize);
      wrap(unknown,unknown,'unknown','未识别文件处理','配置识别不明确时的保存方式。',false);
      wrap(cleanup,cleanup,'cleanup','整理后联动','清理腾空来源子目录，设置STRM生成延时。',false);
      var save=organize.querySelector('[data-cwa="saveNaming"]'),apply=organize.querySelector('[data-cwa="apply"]');wrap(save,apply,'organize-execute','整理执行','按已保存参数立即整理，也可先预览；停止时同时暂停定时整理。',true);
      var correction=top(byId('cw-correction'),organize),matches=top(byId('cw-matches'),organize);if(correction)wrap(correction.previousSibling,matches,'correction','修正作品识别','手动选择作品和季集后重新预览。',false);
      var name=top(byId('cw-rule-name'),organize),rules=top(byId('cw-rules'),organize);if(name)wrap(name.previousSibling,rules,'organize-schedule','自动整理任务','设置Cron或时间间隔，管理已有定时规则。',false);
    }
    existing('cw-link-intake','intake-schedule','接收目录定时整理','勾选频道与接收目录，统一定时归档并联动 STRM。');
    if(strm){var schedule=top(byId('cw-sync-rule-name'),strm);wrap(schedule,schedule,'strm-schedule','STRM 定时扫描','配置时间表达式、时区和同步任务。',false);}
    if(playback){var last=byId('cw-svc-notifications');wrap(top(byId('cw-public'),playback),last?last.previousSibling:playback.lastChild,'playback','STRM 来源与播放地址','个人与分享输出目录、元数据、反代端口及播放配置。',false);}
    existing('cw-svc-notifications','notifications','通知与入库联动','分别配置转存、整理、STRM、频道与日常维护通知。');
    existing('cw-link-emby','emby','Emby 连接与入库确认','管理独立连接和路径映射，查看实际入库核验结果。');
    existing('cw-link-reader','telegram-reader','Telegram 用户会话','登录用户会话，读取频道历史并持续增量监控。');
    if(channels){var main=top(byId('cw-svc-channel-name'),channels),heading=Array.from(channels.children).find(function(n){return n.tagName==='H4'&&n.textContent.includes('TMDB');});if(main&&heading)wrap(main,heading.previousSibling,'channel','频道与保存目录','选择读取方式、历史条数、保存目录与关键词规则。',true);var clear=channels.querySelector('[data-svc="clearTmdb"]');if(heading&&clear)wrap(heading,clear,'channel-tmdb','TMDB可视化筛选','选择类型、地区和具体作品，保存频道规则后生效。',false);}
    [accounts,transfer,share,organize,strm,playback,channels].forEach(toolbar);
  }
  return {mount:mount,reveal:reveal,openLogs:openLogs};
})();
