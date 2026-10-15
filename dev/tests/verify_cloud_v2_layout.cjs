// UI V2 structural smoke test; the browser interaction test is documented separately.
const fs = require('fs'), path = require('path'), assert = require('assert');
const root = path.resolve(__dirname, '../..');
const js = fs.readFileSync(path.join(root, 'static/js/cloud-v2-layout.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static/css/cloud-v2-layout.css'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
assert(html.includes('/static/css/cloud-v2-layout.css?v='));
assert(html.includes('/static/js/cloud-v2-layout.js?v='));
assert(html.indexOf('cloud-workbench.js') < html.indexOf('cloud-v2-layout.js'));
assert(html.indexOf('cloud-v2-layout.js') < html.indexOf('bootstrap.js'));
assert(js.includes('originalLoad.apply(this,arguments)'));
assert(js.includes('localNav.hidden=true')&&js.includes('pageChanged'));
const runtime=fs.readFileSync(path.join(root,'static/js/runtime.js'),'utf8');
assert(runtime.includes('nav-drive-children')&&runtime.includes('data-drive-page'));
assert(!js.includes('cw-v2-mode-tabs'),'no horizontal work area switching');
assert(!/\b(?:fetch|apiCall)\s*\(\s*['"`]\/api\/cloud\/(?:transfers|plans\/apply|shares\/strm\/apply|resources\/apply)/.test(js),'UI patch must not write to cloud APIs');
assert(css.includes('--brand') && css.includes('@media(max-width:610px)'));
console.log('PASS My Drive V2 static asset inclusion, DOM preservation and viewport styling');
