
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
