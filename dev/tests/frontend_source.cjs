// Expand the shipped assets so existing behavior/XSS checks inspect actual code.
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '../../static');
function asset(url) {
  const pathname = url.split('?')[0];
  if (!pathname.startsWith('/static/')) throw Error('unexpected asset URL');
  const file = path.resolve(root, pathname.slice('/static/'.length));
  if (!file.startsWith(root + path.sep)) throw Error('asset outside static root');
  return fs.readFileSync(file, 'utf8');
}
function loadFrontend() {
  let html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  html = html.replace(/<link\b[^>]*href="([^"]+)"[^>]*>/g,
    (tag, url) => /rel="stylesheet"/.test(tag) ? '<style>' + asset(url) + '</style>' : tag);
  html = html.replace(/<script\b[^>]*src="([^"]+)"[^>]*><\/script>/g,
    (tag, url) => '<script>' + asset(url) + '</script>');
  // Classic scripts share a global scope. Preserve contiguous source slices
  // used by the pre-split regression suites across the new file boundaries.
  return html.replace(/<\/script>\s*<script>/g, '\n');
}
module.exports = {loadFrontend, asset};
