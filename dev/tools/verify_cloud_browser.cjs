/* Real browser + shipped HTML/CSS/JS. APIs are explicit fixtures, never a real account. */
const fs=require('fs'),path=require('path'),assert=require('assert'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),out=process.env.SCREENSHOT_DIR||'/tmp/strimkeep-ui';
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||chromium.executablePath(),args:['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--disable-gpu']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[],writes=[];
 page.on('pageerror',e=>errors.push(e.message));
 async function capture(name){
  await page.mouse.move(0,0);
  await page.evaluate(()=>{document.getElementById('sidebar').classList.remove('open');var overlay=document.getElementById('sidebarOverlay');if(overlay){overlay.classList.remove('show');overlay.hidden=true;}window.scrollTo(0,0);});
  await page.waitForTimeout(450);
  const size=page.viewportSize(),height=await page.evaluate(()=>Math.max(document.documentElement.scrollHeight,document.body.scrollHeight));
  await page.setViewportSize({width:size.width,height:Math.max(size.height,height)});await page.waitForTimeout(100);
  await page.screenshot({path:path.join(out,name),fullPage:false});await page.setViewportSize(size);
 }
 const config={public_url:'http://nas.example:8321',output_root:'/strm/local',emby_output_root:'/strimkeep-strm/local',share_output_root:'/strm/share',share_emby_output_root:'',nfo_enabled:true,proxy_port:8322,proxy_enabled:false,video_extensions:['.mkv','.mp4','.avi','.ts'],metadata_extensions:['.srt','.ass','.nfo'],naming_style:'standard',rename_enabled:true,generate_strm_enabled:true};
 let connections=[{id:'b'.repeat(32),name:'家庭 Emby',url:'http://emby.fixture:8096',enabled:true,notify:true,personal_root:'/strimkeep-strm/local',share_root:'/strimkeep-strm/share',key_saved:true}],connectionRevision=1;
 await page.route('http://strimkeep.test/**',async route=>{
  const url=new URL(route.request().url()),p=url.pathname;
  if(p==='/api/logs'){
   const demo=[
    ['cloud_naming','命名结果已核实：征途.2026.S01E01.第一集.2160p.H.265.mkv',{status:'verified',source_name:'Episode.01.mkv',target_name:'征途.2026.S01E01.第一集.2160p.H.265.mkv',elapsed_ms:1250}],
    ['cloud_strm','STRM 已落盘，等待实际 Emby 入库确认',{status:'published',path:'/strm/local/剧集/征途 (2026) {tmdb-336207}/Season 01/征途.2026.S01E01.strm',asset_id:'b'.repeat(32)}],
    ['cloud_governance','按登记文件 ID 发送 115 删除：旧版本.mkv',{status:'dispatched',file_id:'123456789',sha1:'A'.repeat(40),source_name:'旧版本.mkv',size:1048576000}],
    ['cloud_governance','115 源文件删除已核实：旧版本.mkv',{status:'verified',file_id:'123456789',result:'精确不存在回执 + 完整父目录检查'}],
    ['cloud_governance','NAS STRM 已清理，播放引用已撤销',{status:'revoked',asset_id:'b'.repeat(32)}]
   ].map(([source,message,details],i)=>({id:i+1,ts:'2026-10-16T02:10:0'+i,source,message,level:'INFO',details:{job_id:'a'.repeat(32),...details}}));
   await route.fulfill({json:{logs:demo.filter(r=>r.id>Number(url.searchParams.get('after')||0)&&(!url.searchParams.get('level')||r.level===url.searchParams.get('level'))),cursor:5,degraded:false}});return;
  }
  if(p.startsWith('/api/cloud/')){
   const method=route.request().method();if(method!=='GET'&&!p.endsWith('/naming/details'))writes.push({path:p,data:route.request().postDataJSON()});let body={};
   if(p.endsWith('/settings')){if(method==='POST')Object.assign(config,route.request().postDataJSON());body={settings:config,capabilities:{ffprobe_available:true},proxy:{running:false}};}
   else if(p.endsWith('/shares/preview'))body={preview_id:'share-fixture',items:[{id:'4',name:'电影演示 (2026) - 2160p.WEB-DL.H.265.mkv',is_dir:false}],total:1,has_more:false};
   else if(p.endsWith('/shares/strm/preview'))body={plan:{id:'share-plan-fixture',output_root:'/strm/share',effect:'只写 NAS 分享库，不转存',items:[{source:{id:'4',name:'电影演示 (2026) - 2160p.WEB-DL.H.265.mkv'},media:{kind:'movie',title:'电影演示',directory:'电影/电影演示 (2026) {tmdb-123456}',name:'电影演示 (2026) - 2160p.WEB-DL.H.265.mkv'}}],skipped:[],filtered:[]}};
   else if(p.endsWith('/naming/details'))body={template:{style:'standard',directory:'片名 (年份) {tmdb-ID}',tv_directory:'剧名 (年份) {tmdb-ID}',season_directory:'Season 01',movie_file:'片名 (年份) - 分辨率.来源.动态范围.编码.扩展名',tv_file:'剧名.年份.S01E01.集名.分辨率.编码.扩展名',items:[{kind:'movie',directory:'电影/我不是药神 (2018) {tmdb-123456}',name:'我不是药神 (2018) - 2160p.WEB-DL.HDR10.H.265.mkv'},{kind:'tv',directory:'电视剧/金色 (2026) {tmdb-123456}/Season 01',name:'金色.2026.S01E01.第一集.2160p.H.265.mkv'}],notes:['查看详情不会修改配置或移动文件。','参数之间用点连接，复合信息项内部空格保留。']}};
   else if(p.endsWith('/status'))body={accounts:[{id:'A',uid:'123',name:'演示账号',authorization:{cookie:true}}],proxy:{running:config.proxy_enabled,port:config.proxy_port}};
   else if(p.endsWith('/emby-connections')){if(method==='POST'){const incoming=route.request().postDataJSON().connection;const id=incoming.id||'c'.repeat(32);const previous=connections.find(c=>c.id===id)||{};const saved={...previous,...incoming,id,key_saved:!!incoming.key||!!previous.key_saved};delete saved.key;connections=connections.filter(c=>c.id!==id).concat(saved);connectionRevision++;}body={revision:connectionRevision,items:connections};}
   else if(p.endsWith('/directories'))body={directories:{source:{id:'1',name:'待整理'},target:{id:'2',name:'影视归档'},transfer:{id:'3',name:'分享暂存'}},revision:1};
   else if(p.endsWith('/pipeline'))body={generate:true,checks:[['transfer','转存暂存目录','分享暂存','transfer','receive'],['source','待整理来源','待整理','organize','organize-directories'],['target','网盘归档目录','影视归档','organize','organize-directories'],['tmdb','TMDB 识别','密钥已配置','settings',''],['output','NAS STRM 输出','/strm/local','strm','strm-output']].map(([key,label,detail,page,module])=>({key,label,detail,page,module,ready:true}))};
   else if(p.endsWith('/receiving'))body={revision:0,config:{tg_enabled:false}};
   else if(p.endsWith('/maintenance'))body={revision:0,config:{signin_enabled:false,recycle_enabled:false,signin_cron:'0 8 * * *',recycle_cron:'0 4 * * *',timezone:'Asia/Shanghai'}};
   else if(p.endsWith('/organizer-sources'))body={revision:1,items:[],target:{id:'2',name:'影视归档'}};
   else if(p.endsWith('/jobs'))body={items:[
    {id:'job-fixture-1',kind:'organize',state:'running',message:'正在整理',progress:{phase:'移动归档',current:'征途 · S01E01—E28',processed:16,total:28}},
    {id:'job-fixture-2',kind:'sync',state:'success',message:'STRM 已生成 · 等待 Emby 入库检查'},
    {id:'job-fixture-3',kind:'transfer',state:'success',message:'分享接收已核实 · 1 个作品目录'},
    {id:'job-fixture-4',kind:'organize',state:'needs_review',message:'1 个文件识别不明确，已保留原位'},
    {id:'job-fixture-5',kind:'channel',state:'success',message:'频道资源已接收，等待定时整理'}]};
   else if(p.endsWith('/assets'))body={items:[0,1,2,3].map(i=>({id:('a'+i).padEnd(32,'0'),account_id:'A',state:'active',path:'/strm/local/电影/动画电影/作品演示/作品演示.'+i+'.strm'}))};
   else if(p.endsWith('/ingestion'))body={items:[{id:'receipt-fixture-1',asset_id:'asset-fixture',title:'作品演示',path:'/strimkeep-strm/local/作品演示.strm',state:'confirmed',result:{message:'Emby 已确认入库'}}]};
   else if(p.endsWith('/plans'))body={plan:{id:'preview-fixture',items:[],skipped:[],quality8:{notice:'八维预览测试',comparisons:[{new_name:'<New>.mkv',existing_name:'Old.mkv',suggestion:'新版本更优',dimensions:[{label:'分辨率',new:'2160p',existing:'1080p',reason:'新版本优先',verdict:'new'}]}]}}};
   else if(p.endsWith('/telegram-reader'))body={revision:0,connected:false};
   else if(p.endsWith('/resources/catalog'))body={genres:[],countries:[]};
   else if(p.endsWith('/files'))body={items:[],has_more:false,total:0};
   else body={items:[],revision:0};
   await route.fulfill({json:body});return;
  }
  if(p.startsWith('/static/')){const f=path.resolve(root,'.'+p);if(f.startsWith(root+path.sep)&&fs.existsSync(f)){await route.fulfill({path:f});return;}await route.abort();return;}
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');await route.fulfill({contentType:'text/html',body:html});
 });
 await page.goto('http://strimkeep.test/');
 if(process.env.CJK_FONT)await page.addStyleTag({content:'@font-face{font-family:FixtureCJK;src:url(data:font/otf;base64,'+fs.readFileSync(process.env.CJK_FONT).toString('base64')+')}body,input,button,select,textarea{font-family:FixtureCJK,sans-serif!important}.log-row pre,.log-meta{font-family:ui-monospace,FixtureCJK,monospace!important}'});
 await page.evaluate(()=>{window.__activeTab='cloud';window.$=id=>document.getElementById(id);window.esc=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');window.icon=()=>'';window.updateSideTheme=()=>{};window.toggleSidebar=on=>document.getElementById('sidebar').classList.toggle('open',on);window.showModalConfirm=async()=>false;window.switchTab=id=>{window.lastRoute=id;};document.querySelectorAll('.tab').forEach(n=>n.classList.remove('active'));document.getElementById('tab-cloud').classList.add('active');});
 for(const name of ['cloud-services','cloud-linkage','cloud-panels','cloud-workbench','cloud-v2-layout','cloud-studio'])await page.addScriptTag({path:path.join(root,'static/js/'+name+'.js')});
 const runtime=fs.readFileSync(path.join(root,'static/js/runtime.js'),'utf8');await page.addScriptTag({content:runtime.slice(runtime.indexOf('function renderMenu()'),runtime.indexOf('function switchTab(id)'))});
 await page.evaluate(async()=>{renderMenu();await cloudWorkbench.load();await openDrivePage('organize');});
 await page.locator('#cw-pipeline-checks .cw-pipeline-check').first().waitFor({state:'attached'});
 assert(!await page.locator('#cw-correction').isVisible(),'manual correction starts collapsed');
 await page.evaluate(()=>document.fonts.ready);
 await page.screenshot({path:path.join(out,'drive-desktop.png'),fullPage:true});
 await page.locator('#cw-organize [data-module-target="naming"]').click();
 assert(await page.locator('#cw-naming').isVisible());
 assert(!await page.locator('#cw-categories').isVisible());
 await page.locator('[data-config-key="organize-0"] > summary').click();
 const editor=page.locator('#cw-video-extensions + .cw-token-editor');await editor.locator('.cw-token-entry input').fill('webm');await editor.locator('.cw-token-entry button').click();
 await page.evaluate(()=>openDrivePage('files'));await page.evaluate(()=>openDrivePage('organize'));
 assert((await page.locator('#cw-video-extensions').inputValue()).includes('.webm'));
 await page.locator('#cw-organize [data-module-target="classification"]').click();assert(await page.locator('#cw-categories').isVisible());assert(!await page.locator('#cw-naming').isVisible());
 const beforeTemplate=await page.locator('#cw-region-mode').inputValue();
 await page.locator('[data-cwa="templateDetails"]').click();await page.locator('.cw-template-description').waitFor();
 assert(await page.locator('#cw-region-mode').inputValue()===beforeTemplate);
 assert(writes.length===0,'template details are read-only and cannot modify settings');
 await capture('evidence-template-desktop.png');
 await page.screenshot({path:path.join(out,'drive-classification.png'),fullPage:true});
 await page.evaluate(()=>openDrivePage('strm'));await page.locator('#cw-public').waitFor();assert(await page.locator('#cw-output').isVisible());
 assert(!await page.locator('[data-cwa="sync"]').isVisible());
 await page.locator('#cw-strm [data-module-target="strm-scan"]').click();assert(await page.locator('[data-cwa="sync"]').isVisible());assert((await page.locator('#cw-strm-source').textContent()).includes('影视归档'));
 assert(writes.length===0,'navigation and editing never automatically save or start work');
 await page.locator('#cw-strm [data-module-target="strm-output"]').click();await page.locator('#cw-output').fill('/strm/local-new');await page.locator('#cw-strm .cw-config-toolbar [data-cwa="saveSettings"]').click();
 await page.waitForFunction(()=>document.getElementById('cw-message').textContent.includes('已保存'));
 assert(writes.length===1&&writes[0].data.output_root==='/strm/local-new');
 assert(!('proxy_enabled' in writes[0].data)&&!('notify_enabled' in writes[0].data)&&!('emby_output_root' in writes[0].data),'output save cannot modify proxy, notifications or ingestion mapping');
 // Use real DOM controls: the old node() test stubs invented missing preview elements.
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:900});
  await page.evaluate(()=>{openDrivePage('organize');cloudPanels.open('organize','organize-directories');});
  await page.locator('#cw-organize .cw-config-toolbar [data-link-action="saveOrganize"]').click();
  await page.waitForFunction(()=>document.getElementById('cw-message').textContent.includes('整理配置已保存'));
  assert(await page.locator('#cw-quality8-preview').count()===1,'one real quality preview container must exist');
  await page.evaluate(()=>cloudPanels.open('organize','run'));
  await page.locator('[data-cwa="plan"]').click();
  await page.waitForFunction(()=>document.getElementById('cw-quality8-preview')?.textContent.includes('2160p'));
  assert((await page.locator('#cw-quality8-preview').textContent()).includes('<New>.mkv'));
  assert(await page.locator('#cw-quality8-preview New').count()===0,'comparison names are escaped');
  assert(await page.locator('#cw-quality8-preview').isVisible(),'preview stays in the workbench module');
  await page.evaluate(()=>cloudPanels.open('organize','classification'));
  await page.locator('#cw-organize .cw-config-toolbar [data-link-action="saveOrganize"]').click();
  await page.waitForFunction(()=>document.getElementById('cw-message').textContent.includes('整理配置已保存'));
  assert(await page.locator('#cw-quality8-preview').textContent()==='','saving clears obsolete comparison results');
 }
 assert(writes.filter(w=>w.path.endsWith('/settings')).length===5,'output save and four organizer saves succeeded');
 assert(!(await page.locator('#cw-message').textContent()).includes('null'));
 await page.evaluate(()=>{openDrivePage('playback');cloudPanels.open('playback','notifications');});
 await page.locator('#cw-notify').waitFor({state:'visible'});
 assert(await page.locator('#cw-svc-notifications #cw-notify').count()===1,'notification master belongs beside its module switches');
 assert((await page.locator('#cw-svc-notifications').textContent()).includes('作品 STRM 落盘核实后先发海报'),'notification stages are explained beside their switches');
 await page.locator('#cw-notify').check();await page.locator('#cw-svc-notify-strm').check();
 await page.locator('[data-svc="saveNotifications"]').click();
 await page.waitForFunction(()=>document.getElementById('cw-svc-notification-status').textContent.includes('总开关已开启'));
 assert(config.notify_enabled===true&&config.notify_strm===true,'one notifications save persists master and STRM switch');
 await page.evaluate(()=>openDrivePage('strm'));
 const contrast=await page.locator('#cw-strm .cw-config-save').evaluateAll(ns=>ns.filter(n=>n.getClientRects().length).map(n=>{const s=getComputedStyle(n);return {text:n.textContent,fill:s.webkitTextFillColor,bg:s.backgroundColor,size:parseFloat(s.fontSize)};}));
 assert(contrast.every(c=>c.text.trim()&&c.fill!==c.bg&&c.size>=12),'save buttons need visible text contrasting with the background');
 await page.screenshot({path:path.join(out,'drive-strm.png'),fullPage:true});
 await page.evaluate(()=>{openDrivePage('playback');cloudPanels.open('playback','emby');});
 await page.waitForFunction(()=>document.getElementById('cw-link-emby-url').value==='http://emby.fixture:8096');
 assert(await page.locator('#cw-link-emby-name').isVisible(),'connection name is editable');
 assert(await page.locator('#cw-link-emby-notify').count()===0,'there is no duplicate Emby poster switch');
 await page.locator('#cw-link-emby-url').fill('http://unsaved.fixture:8096');
 await page.evaluate(()=>openDrivePage('strm'));await page.evaluate(()=>openDrivePage('playback'));
 assert(await page.locator('#cw-link-emby-url').inputValue()==='http://unsaved.fixture:8096','page navigation preserves connection drafts');
 await page.locator('[data-link-action="saveEmby"]').click();
 await page.waitForFunction(()=>document.getElementById('cw-link-emby-status').textContent.includes('连接已保存'));
 assert(connections.length===1&&connections[0].id==='b'.repeat(32),'saving a loaded connection updates its ID');
 assert(connections[0].notify===true,'saving preserves the legacy preference without sending confirmation posters');
 await page.locator('[data-link-action="newEmby"]').click();
 await page.locator('#cw-link-emby-name').fill('新建连接演示');await page.locator('#cw-link-emby-url').fill('http://new.fixture:8096');
 await page.locator('#cw-link-emby-key').fill('fixture-only');await page.locator('#cw-link-personal-root').fill('/strimkeep-strm/local');
 await page.locator('[data-link-action="saveEmby"]').click();await page.waitForFunction(()=>document.getElementById('cw-link-emby-status').textContent.includes('连接已保存'));
 await page.locator('[data-link-action="saveEmby"]').click();await page.waitForTimeout(50);
 assert(connections.length===2&&writes.filter(w=>w.path.endsWith('/emby-connections')).at(-1).data.connection.id==='c'.repeat(32),'saving the newly created connection again cannot create a duplicate');
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{document.querySelector('[data-emby-edit]').click();window.scrollTo(0,0);});await page.screenshot({path:path.join(out,'drive-emby-mobile.png'),fullPage:true});
 await page.locator('#cw-playback [data-module-target="playback"]').click();
 assert((await page.locator('.cw-playback-guide').textContent()).includes('8097'));
 await page.locator('#cw-port').fill('18322');await page.locator('[data-cwa="saveProxySettings"]').click();
 await page.waitForFunction(()=>document.getElementById('cw-message').textContent.includes('独立反代设置已保存'));
 const proxyWrite=writes.filter(w=>w.path.endsWith('/settings')).at(-1).data;
 assert(proxyWrite.proxy_port===18322&&!('output_root' in proxyWrite)&&!('notify_enabled' in proxyWrite),'proxy save only modifies proxy configuration');
 await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(out,'drive-playback-mobile.png'),fullPage:true});
 await page.evaluate(()=>openDrivePage('strm'));await page.locator('#cw-strm [data-module-target="strm-scan"]').click();
 assert(!await page.locator('#cw-strm .cw-config-toolbar [data-cwa="saveSettings"]').isVisible(),'scan tab cannot show the output configuration save');
 await page.evaluate(()=>openDrivePage('transfer'));
 assert(await page.locator('#cw-transfer .cw-config-toolbar .cw-config-save').count()===0,'transfer toolbar cannot save unrelated robot settings');
 await page.setViewportSize({width:390,height:844});
 for(const section of ['organize','strm','transfer','accounts','playback','channels']){
  await page.evaluate(p=>openDrivePage(p),section);
  for(const key of await page.locator('#cw-'+section+' [data-module-target]').evaluateAll(ns=>ns.map(n=>n.dataset.moduleTarget))){
   await page.locator('#cw-'+section+' [data-module-target="'+key+'"]').click();
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),section+'/'+key+' overflows mobile width');
  }
 }
 await page.evaluate(()=>{openDrivePage('organize');cloudPanels.open('organize','run');window.scrollTo(0,0);});
 await page.screenshot({path:path.join(out,'drive-mobile.png'),fullPage:true});
 assert(await page.locator('#mobileDock').isVisible());assert(await page.locator('.cw-mobile-dock').count()===0,'reuse the global dock');
 await page.evaluate(()=>document.documentElement.dataset.theme='dark');await page.screenshot({path:path.join(out,'drive-mobile-dark.png'),fullPage:true});
 // Studio is the production shell, not a standalone mockup. Only APIs carry demo data.
 await page.setViewportSize({width:1440,height:1050});await page.evaluate(()=>{document.documentElement.dataset.theme='light';openDrivePage('workspace');window.scrollTo(0,0);});
 await page.waitForFunction(()=>document.getElementById('cw-studio-running').textContent==='1'&&document.querySelectorAll('#cw-studio-recent [data-studio-job]').length===5);
 assert(await page.locator('#cw-studio-recent [data-studio-job]').count()===5);
 assert(await page.locator('.cw-studio-nav button').count()===6);
 assert((await page.locator('#cw-studio-attention').textContent())==='1');
 await capture('studio-workspace-desktop.png');
 await page.evaluate(()=>{openDrivePage('configuration');window.scrollTo(0,0);});
 assert(await page.locator('.cw-studio-setup:visible').count()===10);
 await capture('studio-settings-desktop.png');
 await page.locator('#cw-studio-filter').fill('Emby');assert(await page.locator('.cw-studio-setup:visible').count()===1);
 await page.locator('.cw-studio-setup:visible').click();assert(await page.locator('#cw-link-emby-url').isVisible());
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{openDrivePage('workspace');window.scrollTo(0,0);});
 await page.waitForFunction(()=>document.getElementById('cw-studio-running').textContent==='1');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'studio mobile overflow');
 await capture('studio-workspace-mobile.png');
 await page.evaluate(()=>{openDrivePage('configuration');document.getElementById('cw-studio-filter').value='';document.getElementById('cw-studio-filter').dispatchEvent(new Event('input'));window.scrollTo(0,0);});
 await capture('studio-settings-mobile.png');
 await page.locator('.cw-studio-setup[data-studio-module="emby"]').click();
 await page.evaluate(()=>window.scrollTo(0,0));await capture('studio-emby-mobile.png');
 await page.setViewportSize({width:1440,height:1050});
 await page.evaluate(()=>{document.documentElement.dataset.theme='dark';openDrivePage('workspace');window.scrollTo(0,0);});
 await page.waitForFunction(()=>document.querySelectorAll('#cw-studio-recent [data-studio-job]').length===5);
 await capture('studio-workspace-dark.png');
 await page.evaluate(()=>{document.documentElement.dataset.theme='light';openDrivePage('organize');cloudPanels.open('organize','organize-directories');window.scrollTo(0,0);});
 await capture('studio-organize-desktop.png');
 await page.evaluate(()=>{openDrivePage('share-library');});
 assert(!await page.locator('#cw-share-repair').isVisible(),'repair form is not shown before identification');
 assert(await page.locator('#cw-share-auto-organize').count()===0,'share direct output cannot show a transfer mutation checkbox');
 await page.locator('#cw-share-library-link').fill('https://115.com/s/fixtureabc');await page.locator('#cw-share-library [data-cwa="preview"]').click();
 try{await page.locator('#cw-share-list [data-share]').waitFor({timeout:5000});}catch(error){console.error(await page.evaluate(()=>({message:document.getElementById('cw-message').textContent,read:document.getElementById('cw-share-read-status')?.textContent,body:document.getElementById('cw-share-library').innerHTML})));console.error(errors);throw error;}await page.locator('#cw-share-list [data-share]').check();
 await page.locator('#cw-share-library [data-cwa="shareStrmPreview"]').first().click();await page.locator('#cw-share-step-result').waitFor({state:'visible'});
 assert(!await page.locator('#cw-share-season').isVisible(),'movie correction cannot show season/episode fields');
 assert(!await page.locator('#cw-share-library [data-cwa="shareStrmApply"]').isDisabled());
 await capture('share-flow-desktop.png');
 await page.setViewportSize({width:390,height:844});await capture('share-flow-mobile.png');
 await page.locator('#cw-share-repair > summary').click();assert(!await page.locator('#cw-share-season').isVisible());
 await page.locator('#cw-share-kind').selectOption('tv');assert(await page.locator('#cw-share-season').isVisible());
 await page.setViewportSize({width:1440,height:1050});
 await page.addScriptTag({path:path.join(root,'static/js/records.js')});
 await page.evaluate(async()=>{window.api=async p=>(await fetch(p)).json();window.__activeTab='history';document.documentElement.dataset.theme='light';document.querySelectorAll('.tab').forEach(n=>n.classList.toggle('active',n.id==='tab-history'));await loadRecords();document.querySelector('.log-detail').open=true;document.getElementById('recordsList').scrollTop=0;});
 assert(await page.locator('#recordsList .log-row').count()===5);
 assert((await page.locator('#recordsList').textContent()).includes('已发送，待核实'));
 await capture('evidence-logs-desktop.png');
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'log evidence mobile overflow');
 await capture('evidence-logs-mobile.png');
 await page.evaluate(()=>{stopLogCenter();window.__activeTab='cloud';document.querySelectorAll('.tab').forEach(n=>n.classList.toggle('active',n.id==='tab-cloud'));});
 const buttonAudit=[];
 for(const theme of ['light','dark'])for(const width of [390,1440]){
  await page.setViewportSize({width,height:900});await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
  for(const section of ['accounts','files','transfer','share-library','organize','strm','playback','channels','inbox','jobs']){
   await page.evaluate(p=>openDrivePage(p),section);
   const modules=await page.locator('#cw-'+section+' [data-module-target]').evaluateAll(ns=>ns.map(n=>n.dataset.moduleTarget));
   for(const key of modules.length?modules:['']){
    if(key)await page.locator('#cw-'+section+' [data-module-target="'+key+'"]').click();
    await page.locator('#cw-'+section+' details:not(.cw-toolbar-more)').evaluateAll(ns=>ns.forEach(n=>n.open=true));
    buttonAudit.push(...await page.locator('#cw-'+section+' button').evaluateAll((ns,meta)=>ns.filter(n=>n.getClientRects().length).map(n=>{const s=getComputedStyle(n);return {...meta,text:n.textContent.trim().slice(0,50),class:n.className,fill:s.webkitTextFillColor,color:s.color,bg:s.backgroundColor,opacity:s.opacity,font:s.fontSize,disabled:n.disabled};}),{theme,width,section,module:key}));
   }
  }
 }
 for(const theme of ['light','dark']){
  await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
  const tabs=await page.locator('.tab').evaluateAll(ns=>ns.map(n=>n.id));
  for(const tab of tabs){
   await page.evaluate(id=>{document.querySelectorAll('.tab').forEach(n=>n.classList.toggle('active',n.id===id));document.querySelectorAll('#'+id+' details:not(.cw-toolbar-more)').forEach(n=>n.open=true);},tab);
   buttonAudit.push(...await page.locator('#'+tab+' .btn').evaluateAll((ns,meta)=>ns.filter(n=>n.getClientRects().length).map(n=>{const s=getComputedStyle(n);return {...meta,text:n.textContent.trim().slice(0,50)||n.getAttribute('aria-label')||n.getAttribute('title')||n.dataset.icon||'',class:n.className,fill:s.webkitTextFillColor,color:s.color,bg:s.backgroundColor,opacity:s.opacity,font:s.fontSize,disabled:n.disabled};}),{theme,width:1440,section:tab,module:'static-shell'}));
  }
 }
 await page.evaluate(()=>{var parent=document.createElement('div');parent.id='button-inheritance-fixture';parent.style.webkitTextFillColor='transparent';parent.innerHTML='<button class="btn">保存配置</button><button class="btn gray">取消</button><button class="btn red">删除</button>';document.body.append(parent);});
 const inherited=await page.locator('#button-inheritance-fixture .btn').evaluateAll(ns=>ns.map(n=>getComputedStyle(n).webkitTextFillColor));assert(inherited.every(v=>v!=='rgba(0, 0, 0, 0)'),'shared buttons cannot inherit transparent text from a parent');
 await page.locator('#button-inheritance-fixture').evaluate(n=>n.remove());
 fs.writeFileSync(path.join(out,'button-audit.json'),JSON.stringify(buttonAudit,null,2));
 assert(buttonAudit.every(c=>c.text&&c.fill!=='rgba(0, 0, 0, 0)'&&c.fill!==c.bg&&parseFloat(c.font)>=11),'every visible drive button needs readable text');
 assert.deepStrictEqual(errors,[]);
 console.log('PASS real Chromium: desktop/mobile modules, extension edits, unsaved drafts, output save, archive scan default, all module widths, no page errors. Fixture APIs only.');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
