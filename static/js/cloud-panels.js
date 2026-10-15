/* Progressive layout: move existing live controls, never clone or rebuild their handlers. */
window.cloudPanels=(function(){
  'use strict';
  var cards=[],context,sections={},request=0,tokenEditors=[];
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
  function reveal(node){var card=node.closest('[data-cw-module]');if(card)select(card.closest('section'),card.dataset.cwModule);for(var p=node;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;node.scrollIntoView({behavior:'smooth',block:'center'});if(node.focus)node.focus({preventScroll:true});}
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
    function find(){var value=input.value.trim().toLowerCase();if(!value){status.textContent='输入目录、命名、媒体信息等关键词。';return;}var found=Array.from(section.querySelectorAll('.cw-config-card')).find(function(c){return c.textContent.toLowerCase().includes(value);});if(found){reveal(found);status.textContent='已定位匹配模块。';}else status.textContent='本页没有匹配的设置。';}
    search.onclick=find;input.onkeydown=function(event){if(event.key==='Enter'){event.preventDefault();find();}};
    expand.onclick=function(){section.querySelectorAll('.cw-config-card').forEach(function(c){c.open=true;});more.open=false;};collapse.onclick=function(){section.querySelectorAll('.cw-config-card').forEach(function(c){c.open=false;});more.open=false;};
    logs.onclick=openLogs;
    var more=el('details','cw-toolbar-more'),moreTitle=el('summary','','更多'),menu=el('div');menu.append(expand,collapse,logs);more.append(moreTitle,menu);bar.append(label,search,more);
    more.addEventListener('keydown',function(event){if(event.key==='Escape'){more.open=false;moreTitle.focus();}});document.addEventListener('click',function(event){if(!more.contains(event.target))more.open=false;});
    var original=section.querySelector('.cw-organize-toolbar');
    if(original){original.querySelectorAll('label').forEach(function(n){n.hidden=true;});bar.append(original);}
    bar.append(status);section.insertBefore(bar,section.children[1]||null);
    section.querySelectorAll('.cw-config-body').forEach(function(body){body.querySelectorAll('button').forEach(function(b){if(/^保存|检测并保存/.test(b.textContent))b.classList.add('cw-config-save');});});
  }
  function mount(ctx){
    context=ctx;
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
    if(channels){var saveChannel=channels.querySelector('[data-svc="saveChannel"]'),channelStatus=byId('cw-svc-channel-status');if(saveChannel&&channelStatus)wrap(saveChannel,channelStatus,'channel-rules','已保存规则与试算','保存当前频道、编辑已有规则并试算消息。',true);}
    moveOutput(strm);
    [accounts,transfer,share,organize,strm,playback,channels].forEach(toolbar);
    moduleLayout(organize,[['run','整理工作台',['organize-execute','correction']],['organize-directories','来源与归档',['organize-directories','unknown']],['naming','识别与命名',['organize-1','organize-0']],['classification','分类目录',['organize-2']],['quality','版本策略',['organize-3']],['automation','自动化',['cleanup','organize-schedule','intake-schedule']]],'run');
    moduleLayout(strm,[['strm-output','输出与播放地址',['strm-output']],['strm-scan','扫描与生成',['strm-scan']],['strm-schedule','定时同步',['strm-schedule']]],'strm-output');
    moduleLayout(transfer,[['receive','分享链接转存',['receive']],['intake','磁力与机器人',['intake']]],'receive');
    moduleLayout(accounts,[['login','账号登录',['login']],['maintenance','日常维护',['maintenance']]],'login');
    moduleLayout(playback,[['emby','Emby 入库连接',['emby']],['playback','反代服务',['playback']],['notifications','任务通知',['notifications']]],'emby');
    moduleLayout(channels,[['channel','频道与目录',['channel','channel-rules']],['channel-tmdb','作品筛选',['channel-tmdb']],['telegram-reader','用户会话',['telegram-reader']]],'channel');
    if(organize)Array.from(organize.children).forEach(function(n){if(n.tagName==='P'&&n.textContent.startsWith('①'))n.hidden=true;});
    [['video-extensions',false],['metadata-extensions',false],['custom-categories',true],['ignore-keywords',true]].forEach(function(row){tokens(row[0],row[1]);});
    var outputBar=strm&&strm.querySelector('.cw-config-toolbar');if(outputBar){var outputSave=el('button','btn cw-config-save','保存输出配置');outputSave.type='button';outputSave.dataset.cwa='saveSettings';outputBar.append(outputSave);}
    dashboard(organize);
  }
  function select(section,key){
    if(!section||!sections[section.id])return;
    sections[section.id].active=key;
    section.querySelectorAll('[data-cw-module]').forEach(function(n){n.hidden=n.dataset.cwModule!==key;if(!n.hidden&&n.tagName==='DETAILS'&&!['correction','organize-0'].includes(n.dataset.configKey))n.open=true;});
    section.querySelectorAll('[data-module-target]').forEach(function(b){var on=b.dataset.moduleTarget===key;b.classList.toggle('on',on);b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;});
  }
  function moduleLayout(section,definitions,initial){
    if(!section)return;
    sections[section.id]={active:initial};var nav=el('div','cw-module-nav');nav.setAttribute('role','tablist');nav.setAttribute('aria-label','配置模块');
    definitions.forEach(function(def){var b=el('button','',def[1]);b.type='button';b.dataset.moduleTarget=def[0];b.setAttribute('role','tab');b.onclick=function(){select(section,def[0]);};nav.append(b);
      def[2].forEach(function(key){var card=section.querySelector('[data-config-key="'+key+'"]');if(card)card.dataset.cwModule=def[0];});
    });
    nav.onkeydown=function(event){if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;var buttons=Array.from(nav.children),i=buttons.indexOf(document.activeElement);i=event.key==='Home'?0:event.key==='End'?buttons.length-1:(i+(event.key==='ArrowLeft'?-1:1)+buttons.length)%buttons.length;event.preventDefault();buttons[i].click();buttons[i].focus();};
    section.insertBefore(nav,section.querySelector('.cw-config-card'));section.classList.add('cw-modular');select(section,initial);
  }
  function moveOutput(strm){
    if(!strm)return;var box=el('details');box.dataset.configKey='';var body=el('div');
    ['public','output','emby-output','share-output','share-emby-output','nfo','artwork'].forEach(function(key){var n=byId('cw-'+key);if(n)body.append(n.closest('label'));});
    var note=el('p','hint');note.id='cw-output-note';body.prepend(note);
    body.prepend(el('p','hint','本项目生成的 STRM 默认独立保存到 /strm/local 和 /strm/share，对应部署目录下 strm/local、strm/share。原 /media/local、/media/share 继续用于治理已有上游 STRM。先挂载 ./strm:/strm:rw，再填写这里的容器内路径。服务地址必须能被播放器访问。'));
    var save=el('button','btn gray','保存输出配置');save.type='button';save.dataset.cwa='saveSettings';body.append(save);Array.from(body.childNodes).forEach(function(n){box.append(n);});strm.insertBefore(box,strm.children[1]);enhance(box,'strm-output','STRM 输出与播放地址','设置 NAS 输出目录、稳定播放入口与元数据。',true);
    var source=top(byId('cw-strm-source'),strm),schedule=top(byId('cw-sync-rule-name'),strm);
    if(source&&schedule){wrap(source,schedule.previousSibling,'strm-scan','扫描与生成','独立扫描网盘文件；不改名、不移动视频。',true);}
    // History belongs to the scan module, rather than leaking into every settings tab.
    var assets=byId('cw-assets');if(assets){var end=strm.lastChild;wrap(assets,end,'strm-assets','已生成记录','查看本项目生成的 STRM。',true);var card=strm.querySelector('[data-config-key="strm-assets"]');if(card)card.dataset.cwModule='strm-scan';}
  }
  function dashboard(section){
    if(!section)return;var panel=el('div','cw-pipeline');panel.dataset.cwModule='run';
    panel.append(el('h3','','资源处理链路'),el('p','hint','转存到暂存目录 → TMDB 识别与命名 → 移动到网盘归档 → 写入 NAS STRM。每一步的配置与执行结果在下方查看。'));
    var status=el('div','cw-pipeline-checks');status.id='cw-pipeline-checks';status.setAttribute('aria-live','polite');panel.append(status);
    var actions=el('div','cw-pipeline-actions');[['transfer','receive','链接转存'],['organize','organize-directories','配置网盘目录'],['strm','strm-output','配置 STRM 输出'],['jobs','','任务与结果']].forEach(function(row){var b=el('button','btn gray',row[2]);b.type='button';b.onclick=function(){open(row[0],row[1]);};actions.append(b);});
    var refreshButton=el('button','btn gray','重新检查配置');refreshButton.type='button';refreshButton.onclick=function(){context.guard(refresh);};actions.append(refreshButton);panel.append(actions);section.insertBefore(panel,section.querySelector('.cw-config-card'));refresh();
  }
  function open(page,key){if(page==='settings'){if(typeof switchTab==='function')switchTab('settings');return;}window.cloudWorkbench.navigate(page);var section=byId('cw-'+page);if(key)select(section,key);}
  async function refresh(){
    if(!context)return;var node=byId('cw-pipeline-checks');if(!node)return;var account=context.state.account,epoch=context.state.epoch,token=++request;
    if(!account){node.textContent='先在网盘配置中登录账号，再设置目录。';return;}
    node.textContent='正在检查配置…';
    try{var result=await context.api('/accounts/'+account+'/pipeline');if(token!==request||account!==context.state.account||epoch!==context.state.epoch)return;
      node.replaceChildren();(result.checks||[]).forEach(function(c){var b=el('button','cw-pipeline-check '+(c.ready?'is-ready':'is-missing'));b.type='button';b.append(el('span','',c.ready?'已配置':'待配置'),el('strong','',c.label),el('small','',c.detail));b.onclick=function(){open(c.page,c.module);};node.append(b);});
      node.append(el('p','cw-pipeline-note',result.generate?'整理后自动生成 STRM；这里检查本机配置，远端连接与播放结果以任务记录为准。':'当前仅整理网盘文件，已关闭整理后生成 STRM。'));
    }catch(error){if(token===request)node.textContent=error.message;}
  }
  function pageChanged(page){if(['organize','transfer','strm'].includes(page))refresh();}
  function tokens(key,multiline){
    var source=byId('cw-'+key);if(!source)return;var box=el('div','cw-token-editor'),list=el('div','cw-token-list'),entry=el('div','cw-token-entry'),input=el('input'),add=el('button','btn gray','添加');
    var label=source.closest('label');label.childNodes[0].textContent={'video-extensions':'视频文件类型','metadata-extensions':'附属文件类型','custom-categories':'自定义分类','ignore-keywords':'忽略关键词'}[key];
    input.type='text';input.setAttribute('aria-label','添加'+source.closest('label').childNodes[0].textContent.trim());input.placeholder=multiline?'输入一项后添加':'例如 mkv，输入后添加';add.type='button';entry.append(input,add);box.append(list,entry);source.hidden=true;source.after(box);
    function values(){return source.value.split(multiline?'\n':/[\s,，]+/).map(function(v){return v.trim();}).filter(Boolean);}
    function write(items){source.value=items.join(multiline?'\n':',');source.dispatchEvent(new Event('input',{bubbles:true}));source.dispatchEvent(new Event('change',{bubbles:true}));render();}
    function render(){list.replaceChildren();values().forEach(function(v,index){var item=el('span','cw-token'),text=el('span','',v),remove=el('button','','×');remove.type='button';remove.setAttribute('aria-label','移除 '+v);remove.onclick=function(){var items=values();items.splice(index,1);write(items);};item.append(text,remove);list.append(item);});}
    function insert(){var raw=input.value.trim();if(!raw)return;var extra=multiline?[raw]:raw.split(/[\s,，]+/).filter(Boolean).map(function(v){return '.'+v.replace(/^\.+/,'').toLowerCase();}),items=values();extra.forEach(function(v){if(!items.includes(v))items.push(v);});write(items);input.value='';input.focus();}
    add.onclick=insert;input.onkeydown=function(event){if(event.key==='Enter'){event.preventDefault();insert();}};tokenEditors.push(render);render();
  }
  function settingsLoaded(){tokenEditors.forEach(function(render){render();});}
  return {mount:mount,reveal:reveal,openLogs:openLogs,pageChanged:pageChanged,refresh:refresh,open:open,settingsLoaded:settingsLoaded};
})();
