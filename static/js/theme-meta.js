
/* 状态栏颜色跟随主题 */
(function(){
  var meta=document.querySelector('meta[name="theme-color"]');
  function sync(){
    var t=document.documentElement.getAttribute('data-theme');
    var dark=t?t==='dark':!!(window.matchMedia&&matchMedia('(prefers-color-scheme:dark)').matches);
    if(meta)meta.setAttribute('content',dark?'#101419':'#e4f2fc');
  }
  sync();
  new MutationObserver(sync).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  if(window.matchMedia)matchMedia('(prefers-color-scheme:dark)').addEventListener('change',sync);
})();

/* Background addresses are saved on the server; this cache only avoids repeat flashes. */
var appearanceRevision = 0, backgroundGeneration = 0, appearanceBusy = false;
var appearanceSaved = {background_light_url:'',background_dark_url:''};
var backgroundLoaded = {light:'',dark:''};
function backgroundAddress(value){
  var text=String(value||'').trim();
  if(!text)return '';
  var u;
  try{u=new URL(text);}catch(e){throw new Error('请填写有效的图片直链');}
  if(text.length>2048||!/^https?:$/.test(u.protocol)||u.username||u.password||/[\s\\"<>\x00-\x1f\x7f]/.test(text))throw new Error('请使用 HTTP/HTTPS 图片直链');
  if(window.location&&window.location.protocol==='https:'&&u.protocol==='http:')throw new Error('当前为 HTTPS 页面，请使用 HTTPS 图片地址');
  return text;
}
function appearanceDraft(){
  return {background_light_url:backgroundAddress($('background-light-url').value),background_dark_url:backgroundAddress($('background-dark-url').value)};
}
function fillAppearanceFields(state){
  ['light','dark'].forEach(function(theme){var el=$('background-'+theme+'-url');if(el)el.value=state['background_'+theme+'_url']||'';});
}
function applyAppearance(state){
  var generation=++backgroundGeneration,root=document.documentElement;
  return Promise.all(['light','dark'].map(function(theme){
    var property='--glass-'+theme+'-image',url;
    try{url=backgroundAddress(state['background_'+theme+'_url']);}catch(e){url='';}
    if(url&&backgroundLoaded[theme]===url)return true;
    backgroundLoaded[theme]='';root.style.removeProperty(property);
    if(!url)return !state['background_'+theme+'_url'];
    return new Promise(function(resolve){
      var image=new Image(),done=false;
      var timer=setTimeout(function(){finish(false);},10000);
      function finish(ok){
        if(done)return;done=true;clearTimeout(timer);image.onload=image.onerror=null;
        if(generation===backgroundGeneration&&ok){backgroundLoaded[theme]=url;root.style.setProperty(property,'url('+JSON.stringify(url)+')');}
        resolve(ok&&generation===backgroundGeneration);
      }
      image.referrerPolicy='no-referrer';image.onload=function(){finish(image.naturalWidth>0);};image.onerror=function(){finish(false);};image.src=url;
    });
  }));
}
async function loadAppearance(fill){
  var revision=appearanceRevision;
  try{
    var r=await api('/api/appearance',{timeoutMs:5000});
    if(r.status!=='success'||revision!==appearanceRevision)return;
    appearanceSaved=r.appearance||{};
    try{localStorage.setItem('strimkeep-backgrounds',JSON.stringify(appearanceSaved));}catch(e){}
    if(fill)fillAppearanceFields(appearanceSaved);
    await applyAppearance(appearanceSaved);
  }catch(e){if(fill&&revision===appearanceRevision){fillAppearanceFields(appearanceSaved);$('backgroundResult').textContent='读取背景设置失败，请稍后重试';}}
}
function backgroundButtons(busy){
  appearanceBusy=busy;
  ['backgroundPreviewBtn','backgroundSaveBtn','backgroundResetBtn'].forEach(function(id){var el=$(id);if(el)el.disabled=busy;});
}
async function previewAppearance(){
  if(appearanceBusy)return;
  var draft,revision;
  try{draft=appearanceDraft();}catch(e){$('backgroundResult').textContent=e.message;return;}
  revision=++appearanceRevision;backgroundButtons(true);$('backgroundResult').textContent='正在加载预览…';
  try{
    var loaded=await applyAppearance(draft);
    if(revision===appearanceRevision)$('backgroundResult').textContent=loaded.every(Boolean)?'正在预览，尚未保存；可切换深浅模式查看。':'部分图片无法加载，已使用内置背景。请检查直链或图床访问限制。';
  }finally{backgroundButtons(false);}
}
async function saveAppearance(reset){
  if(appearanceBusy)return;
  var draft,revision;
  try{draft=reset?{background_light_url:'',background_dark_url:''}:appearanceDraft();}catch(e){$('backgroundResult').textContent=e.message;return;}
  revision=++appearanceRevision;backgroundButtons(true);$('backgroundResult').textContent='正在保存背景…';
  try{
    var r=await api('/api/appearance',{method:'POST',body:JSON.stringify(draft),timeoutMs:8000});
    if(r.status!=='success')throw new Error(r.message||'保存失败');
    appearanceSaved=r.appearance||draft;
    try{localStorage.setItem('strimkeep-backgrounds',JSON.stringify(appearanceSaved));}catch(e){}
    if(revision!==appearanceRevision)return;
    fillAppearanceFields(appearanceSaved);
    var loaded=await applyAppearance(appearanceSaved);
    if(revision===appearanceRevision)$('backgroundResult').textContent=loaded.every(Boolean)?(reset?'已恢复内置背景。':'背景已保存，手机和电脑重新打开页面后同步。'):'地址已保存，但部分图片无法加载，暂用内置背景；请更换可直接访问的图片地址。';
  }catch(e){if(revision===appearanceRevision)$('backgroundResult').textContent='保存失败：'+e.message;}
  finally{backgroundButtons(false);}
}
function resetAppearance(){return saveAppearance(true);}
(function(){
  try{var saved=JSON.parse(localStorage.getItem('strimkeep-backgrounds')||'null');if(saved){appearanceSaved=saved;applyAppearance(saved);}}catch(e){}
})();
