#!/usr/bin/env node
// Check the shipped entry points, then run every frontend regression suite.
const fs = require('fs'), path = require('path'), vm = require('vm');
const {spawnSync} = require('child_process');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
for (const file of fs.readdirSync(path.join(root, 'static/js')).filter(f => f.endsWith('.js'))) {
  const result = spawnSync(process.execPath, ['--check', path.join(root, 'static/js', file)], {stdio:'inherit'});
  if (result.status !== 0) process.exit(result.status || 1);
}
for (const [i, match] of Array.from(html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)).entries()) {
  if (!/\bsrc\s*=/.test(match[1])) new vm.Script(match[2], {filename:'inline-'+i});
}
const suites = fs.readdirSync(path.join(root, 'dev/tests')).filter(f => /^verify_.*\.cjs$/.test(f)).sort();
for (const suite of suites) {
  const result = spawnSync(process.execPath, [path.join(root, 'dev/tests', suite)], {stdio:'inherit', cwd:root});
  if (result.status !== 0) process.exit(result.status || 1);
}
console.log(`PASS frontend syntax and ${suites.length} regression suites; DOM substitutes, no browser visual claims`);
