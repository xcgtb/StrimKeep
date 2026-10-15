/* Real browser + shipped HTML/CSS/JS. APIs are explicit fixtures, never a real account. */
const fs=require('fs'),path=require('path'),assert=require('assert'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),out=process.env.SCREENSHOT_DIR||'/tmp/strimkeep-ui';
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||chromium.executablePath(),args:['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--disable-gpu']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[],writes=[];
 page.on('pageerror',e=>errors.push(e.message));
 const config={public_url:'http://nas.example:8321',output_root:'/strm/local',emby_output_root:'/strimkeep-strm/local',share_output_root:'/strm/share',share_emby_output_root:'',nfo_enabled:true,proxy_port:8322,proxy_enabled:false,video_extensions:['.mkv','.mp4','.avi','.ts'],metadata_extensions:['.srt','.ass','.nfo'],naming_style:'standard',rename_enabled:true,generate_strm_enabled:true};
 await page.route('http://strimkeep.test/**',async route=>{
  const url=new URL(route.request().url()),p=url.pathname;
  if(p.startsWith('/api/cloud/')){
   const method=route.request().method();if(method!=='GET')writes.push({path:p,data:route.request().postDataJSON()});let body={};
   if(p.endsWith('/settings')){if(method==='POST')Object.assign(config,route.request().postDataJSON());body={settings:config,capabilities:{ffprobe_available:true},proxy:{running:false}};}
   else if(p.endsWith('/status'))body={accounts:[{id:'A',uid:'123',name:'演示账号',authorization:{cookie:true}}],proxy:{running:false}};
   else if(p.endsWith('/directories'))body={directories:{source:{id:'1',name:'待整理'},target:{id:'2',name:'影视归档'},transfer:{id:'3',name:'分享暂存'}},revision:1};
   else if(p.endsWith('/pipeline'))body={generate:true,checks:[['transfer','转存暂存目录','分享暂存','transfer','receive'],['source','待整理来源','待整理','organize','organize-directories'],['target','网盘归档目录','影视归档','organize','organize-directories'],['tmdb','TMDB 识别','密钥已配置','settings',''],['output','NAS STRM 输出','/strm/local','strm','strm-output']].map(([key,label,detail,page,module])=>({key,label,detail,page,module,ready:true}))};
   else if(p.endsWith('/receiving'))body={revision:0,config:{tg_enabled:false}};
   else if(p.endsWith('/maintenance'))body={revision:0,config:{signin_enabled:false,recycle_enabled:false,signin_cron:'0 8 * * *',recycle_cron:'0 4 * * *',timezone:'Asia/Shanghai'}};
   else if(p.endsWith('/organizer-sources'))body={revision:1,items:[],target:{id:'2',name:'影视归档'}};
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
 if(process.env.CJK_FONT)await page.addStyleTag({content:'@font-face{font-family:FixtureCJK;src:url(data:font/otf;base64,'+fs.readFileSync(process.env.CJK_FONT).toString('base64')+')}body,input,button,select,textarea{font-family:FixtureCJK,sans-serif!important}'});
 await page.evaluate(()=>{window.__activeTab='cloud';window.$=id=>document.getElementById(id);window.esc=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');window.icon=()=>'';window.updateSideTheme=()=>{};window.toggleSidebar=on=>document.getElementById('sidebar').classList.toggle('open',on);window.showModalConfirm=async()=>false;window.switchTab=id=>{window.lastRoute=id;};document.querySelectorAll('.tab').forEach(n=>n.classList.remove('active'));document.getElementById('tab-cloud').classList.add('active');});
 for(const name of ['cloud-services','cloud-linkage','cloud-panels','cloud-workbench','cloud-v2-layout'])await page.addScriptTag({path:path.join(root,'static/js/'+name+'.js')});
 const runtime=fs.readFileSync(path.join(root,'static/js/runtime.js'),'utf8');await page.addScriptTag({content:runtime.slice(runtime.indexOf('function renderMenu()'),runtime.indexOf('function switchTab(id)'))});
 await page.evaluate(async()=>{renderMenu();await cloudWorkbench.load();await openDrivePage('organize');});
 await page.locator('#cw-pipeline-checks .cw-pipeline-check').first().waitFor();
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
 await page.locator('[data-cwa="upstreamPreset"]').click();
 assert(await page.locator('#cw-region-mode').inputValue()==='grouped');
 assert(await page.locator('#cw-classification').isChecked()&&await page.locator('#cw-animation-movie-folder').isChecked());
 assert(await page.locator('#cw-categories input:checked').count()===7);
 assert(writes.length===0,'the upstream preset only changes the form until Save');
 await page.screenshot({path:path.join(out,'drive-classification.png'),fullPage:true});
 await page.evaluate(()=>openDrivePage('strm'));await page.locator('#cw-public').waitFor();assert(await page.locator('#cw-output').isVisible());
 assert(!await page.locator('[data-cwa="sync"]').isVisible());
 await page.locator('#cw-strm [data-module-target="strm-scan"]').click();assert(await page.locator('[data-cwa="sync"]').isVisible());assert((await page.locator('#cw-strm-source').textContent()).includes('影视归档'));
 assert(writes.length===0,'navigation and editing never automatically save or start work');
 await page.locator('#cw-strm [data-module-target="strm-output"]').click();await page.locator('#cw-output').fill('/strm/local-new');await page.locator('#cw-strm .cw-config-toolbar [data-cwa="saveSettings"]').click();
 await page.waitForFunction(()=>document.getElementById('cw-message').textContent.includes('已保存'));
 assert(writes.length===1&&writes[0].data.output_root==='/strm/local-new');
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
 await page.locator('#cw-notify').check();await page.locator('#cw-svc-notify-strm').check();
 await page.locator('[data-svc="saveNotifications"]').click();
 await page.waitForFunction(()=>document.getElementById('cw-svc-notification-status').textContent.includes('总开关已开启'));
 assert(config.notify_enabled===true&&config.notify_strm===true,'one notifications save persists master and STRM switch');
 await page.evaluate(()=>openDrivePage('strm'));
 const contrast=await page.locator('#cw-strm .cw-config-save').evaluateAll(ns=>ns.filter(n=>n.getClientRects().length).map(n=>{const s=getComputedStyle(n);return {text:n.textContent,fill:s.webkitTextFillColor,bg:s.backgroundColor,size:parseFloat(s.fontSize)};}));
 assert(contrast.every(c=>c.text.trim()&&c.fill!==c.bg&&c.size>=12),'save buttons need visible text contrasting with the background');
 await page.screenshot({path:path.join(out,'drive-strm.png'),fullPage:true});
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
