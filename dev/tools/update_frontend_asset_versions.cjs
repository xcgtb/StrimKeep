#!/usr/bin/env node
/**
 * Keep cache-busting versions in static/index.html synchronized with assets.
 * Run without arguments to update; pass --check in CI to fail on drift.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '../..');
const INDEX = path.join(ROOT, 'static', 'index.html');
const URL_RE = /\b(?:src|href)="(\/static\/[^"#]+)"/g;
const VERSION_RE = /([?&]v=)[^&#"]*/;

// Git stores these text assets with LF line endings. Normalize CRLF from
// Windows worktrees so version values agree with Linux CI and Docker builds.
const TEXT_EXTENSIONS = new Set(['.css', '.js', '.svg']);
function digest(file) {
  const bytes = fs.readFileSync(file);
  const input = TEXT_EXTENSIONS.has(path.extname(file).toLowerCase())
    ? bytes.toString('utf8').replace(/\r\n/g, '\n')
    : bytes;
  return crypto.createHash('sha256').update(input).digest('hex').slice(0, 12);
}

function assetPath(rawUrl) {
  const parsed = new URL(rawUrl, 'http://strimkeep.local');
  const relative = decodeURIComponent(parsed.pathname).replace(/^\//, '');
  const file = path.resolve(ROOT, relative);
  const staticRoot = path.resolve(ROOT, 'static') + path.sep;
  if (!file.startsWith(staticRoot)) throw new Error(`asset escapes static directory: ${rawUrl}`);
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) throw new Error(`asset does not exist: ${rawUrl}`);
  return file;
}

function collect(html) {
  const updates = [];
  for (const match of html.matchAll(URL_RE)) {
    const rawUrl = match[1];
    const file = assetPath(rawUrl);
    const expected = digest(file);
    const version = rawUrl.match(VERSION_RE);
    if (!version) throw new Error(`asset URL is missing ?v=: ${rawUrl}`);
    const actual = version[0].slice(version[1].length);
    if (actual !== expected) updates.push({rawUrl, nextUrl: rawUrl.replace(VERSION_RE, `$1${expected}`)});
  }
  return updates;
}

function main() {
  const check = process.argv.slice(2).includes('--check');
  const html = fs.readFileSync(INDEX, 'utf8');
  const updates = collect(html);
  if (updates.length === 0) return console.log('frontend asset versions: OK');
  if (check) {
    for (const update of updates) console.error(`${update.rawUrl} -> ${update.nextUrl}`);
    throw new Error(`${updates.length} frontend asset version(s) are stale; run without --check to update them`);
  }
  let next = html;
  for (const update of updates) next = next.split(`"${update.rawUrl}"`).join(`"${update.nextUrl}"`);
  fs.writeFileSync(INDEX, next);
  console.log(`updated ${updates.length} frontend asset version(s)`);
}

try { main(); } catch (error) {
  console.error(`frontend asset versions: ${error.message}`);
  process.exitCode = 1;
}
