
(function(){
  function mergeCards(){
    var groups = document.querySelectorAll('#tab-dashboard > .cards');
    if (groups.length <= 1) return;
    var first = groups[0];
    for (var i = 1; i < groups.length; i++) {
      while (groups[i].firstChild) {
        first.appendChild(groups[i].firstChild);
      }
      groups[i].remove();
    }
  }
  function applyCols(){
    var w = window.innerWidth || document.documentElement.clientWidth;
    var cols = w >= 600 ? 4 : 2;
    document.querySelectorAll('#tab-dashboard .cards').forEach(function(el){
      el.style.setProperty('grid-template-columns', 'repeat(' + cols + ', 1fr)', 'important');
      el.style.setProperty('gap', '12px', 'important');
    });
  }
  function run(){
    mergeCards();
    applyCols();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
  window.addEventListener('resize', applyCols);
  window.addEventListener('orientationchange', function(){ setTimeout(applyCols, 300); });
  setTimeout(run, 500);
  setTimeout(run, 1500);
})();
