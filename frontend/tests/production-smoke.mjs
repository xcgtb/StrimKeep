import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname, join } from 'node:path';
import { chromium } from '@playwright/test';
import { routes } from './e2e/routes.ts';
import { fixtures } from './e2e/fixtures.ts';
const root = resolve('..');
const mime = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.ico': 'image/x-icon',
};
const server = createServer(async (req, res) => {
  const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
  if (!path.startsWith(root + '/')) {
    res.writeHead(403).end();
    return;
  }
  try {
    const data = await readFile(path);
    res.writeHead(200, { 'Content-Type': mime[extname(path)] || 'application/octet-stream' });
    res.end(data);
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}/static/index.html`;
const browser = await chromium.launch({ headless: true });
try {
  for (const [name, viewport] of [
    ['desktop', { width: 1440, height: 1000 }],
    ['mobile', { width: 390, height: 844 }],
  ]) {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage(),
      errors = [],
      missing = [];
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('response', (r) => {
      if (r.status() >= 400 && new URL(r.url()).pathname.startsWith('/static/'))
        missing.push(r.url());
    });
    await fixtures(page);
    for (const [route, heading] of routes) {
      await page.goto(`${base}#/${route}`);
      try {
        await page.getByRole('heading', { name: heading, exact: true }).waitFor({ timeout: 10000 });
      } catch (error) {
        throw new Error(
          JSON.stringify({ route, errors, missing, text: await page.locator('body').innerText() }),
          { cause: error },
        );
      }
      await page.waitForLoadState('networkidle');
      assert.equal(
        await page.getByText('页面出现异常，请刷新页面重试。', { exact: true }).count(),
        0,
        `${name} ${route} handled exception`,
      );
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
        `${name} ${route} overflow`,
      );
      if (
        [
          'dashboard',
          'resources',
          'organize/workspace',
          'settings/cloud',
          'resources/share',
          'organize/naming',
          'organize/quality',
          'logs',
          'workflow',
        ].includes(route) &&
        process.env.STRIMKEEP_SCREENSHOT_DIR
      ) {
        await mkdir(process.env.STRIMKEEP_SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({
          path: join(
            process.env.STRIMKEEP_SCREENSHOT_DIR,
            `vue3-${name}-${route.replaceAll('/', '-')}.png`,
          ),
          fullPage: name === 'desktop',
        });
      }
    }
    assert.deepEqual(errors, [], `${name} page errors`);
    assert.deepEqual(missing, [], `${name} missing built assets`);
    await context.close();
    console.log(
      `PASS ${name}: ${routes.length} built routes, no page errors, no missing chunks, no horizontal overflow`,
    );
  }
} finally {
  await browser.close();
  await new Promise((r) => server.close(r));
}
