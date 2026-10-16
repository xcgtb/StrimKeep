#!/usr/bin/env node
// Validate the delivered Vue entry; browser checks live in frontend/tests.
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
assert(html.includes('type="module"')&&html.includes('<div id="app"></div>'));
assert(!/\/static\/(js|css|vue3)\//.test(html),'legacy frontend must not load');
for(const match of html.matchAll(/(?:src|href)="(\/static\/[^"?#]+)"/g))assert(fs.statSync(path.join(root,match[1])).isFile(),match[1]);
assert(!fs.existsSync(path.join(root,'static/js'))&&!fs.existsSync(path.join(root,'static/css'))&&!fs.existsSync(path.join(root,'static/vue3')));
console.log('PASS Vue production entry, hashed assets and a single frontend');
