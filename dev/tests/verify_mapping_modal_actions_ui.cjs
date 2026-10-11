const assert = require('assert'), vm = require('vm');
const source = require('./frontend_source.cjs').asset('/static/js/mapping.js');
const series = source.slice(source.indexOf('  html += \'<div class="mapping-detail-actions"><div class="mapping-action-tools">\';'), source.indexOf('  openModal(html);', source.indexOf('  html += \'<div class="mapping-detail-actions"><div class="mapping-action-tools">\';')));
assert(series.length > 0);
for (const mst of ['missing','ongoing','complete']) {
  for (const done of [false,true]) {
    for (const host of ['', 'https://emby.example']) {
      const ctx = {html:'',s:{id:'s',name:'剧',_md:done},mst,embyHost:host,esc:String,jsarg:JSON.stringify};
      vm.runInNewContext(series,ctx);
      const rows = ctx.html.match(/<div class="mapping-action-delete-row">([\s\S]*?)<\/div>/);
      assert(rows, 'fixed deletion row required in every state');
      assert(rows[1].indexOf('删除本地库') < rows[1].indexOf('删除分享库'));
      assert(!rows[1].includes('完结') && !rows[1].includes('Emby'));
      assert(ctx.html.includes('mapping-action-close'));
      assert.equal((ctx.html.match(/data-mapping-delete/g) || []).length,2);
    }
  }
}
const movieStart = source.indexOf('  html += \'<div class="mapping-detail-actions"><div class="mapping-action-delete-row">\';');
const movie = source.slice(movieStart,source.indexOf('  openModal(html);',movieStart));
for(const host of ['', 'https://emby.example']) {
  const ctx={html:'',m:{tmdb_id:'1',id:'m',name:'电影'},embyHost:host,esc:String,jsarg:JSON.stringify};
  vm.runInNewContext(movie,ctx);
  const row=ctx.html.match(/<div class="mapping-action-delete-row">([\s\S]*?)<\/div>/)[1];
  assert(row.indexOf('删除本地库') < row.indexOf('删除分享库'));
  assert(!row.includes('Emby') && ctx.html.includes('mapping-action-close'));
}
console.log('PASS actual series/movie action markup keeps delete positions across completion and Emby states');
