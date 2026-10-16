import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('light sidebar follows the final reference and dark mode switches the whole workspace', async ({
  page,
}) => {
  await fixtures(page);
  await page.goto('/#/dashboard');
  await expect(page.getByRole('heading', { name: '运行总览', exact: true })).toBeVisible();
  await expect(page.locator('.sidebar')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await page.getByRole('button', { name: '切换深色主题' }).click();
  await expect(page.locator('.sidebar')).toHaveCSS('background-color', 'rgb(24, 38, 56)');
  await page.goto('/#/organize/naming');
  await expect(page.getByRole('heading', { name: '自定义命名模板', exact: true })).toBeVisible();
  const secondaryButton = page.locator('.naming-variable-card .button:not(.primary)').first();
  await expect(secondaryButton).toHaveCSS('background-color', 'rgb(27, 42, 60)');
  await expect(secondaryButton).toHaveCSS('color', 'rgb(234, 241, 250)');
  await page.getByRole('button', { name: '切换浅色主题' }).click();
  await expect(page.locator('.sidebar')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
});

test('variable insertion preserves the caret and saves only the selected template draft', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/organize/naming');
  const input = page.getByLabel('电影文件名', { exact: true });
  await input.fill('{title}-{ext}');
  await input.evaluate((el: HTMLInputElement) => {
    el.setSelectionRange(7, 7);
    el.dispatchEvent(new Event('select', { bubbles: true }));
  });
  await page.getByLabel('搜索模板变量').fill('年份');
  await page.getByRole('button', { name: '将上映／首播年份插入模板', exact: true }).click();
  await expect(input).toHaveValue('{title}{year}-{ext}');
  expect(state.writes).toHaveLength(0);
  await page.getByRole('button', { name: '保存 1 项修改' }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  expect(state.writes.filter((r) => r.path === '/api/cloud/settings')).toEqual([
    { path: '/api/cloud/settings', body: { movie_file_template: '{title}{year}-{ext}' } },
  ]);
});

test('eight-dimensional ordering and switches save separately from seven-dimensional governance', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/organize/quality');
  await page.getByRole('checkbox', { name: '分辨率', exact: true }).uncheck();
  await page.getByRole('button', { name: '上移真实源文件大小', exact: true }).click();
  await page.getByRole('button', { name: '查看分辨率档位', exact: true }).click();
  const rule = page
    .locator('.quality-rule')
    .filter({ has: page.getByRole('checkbox', { name: '分辨率', exact: true }) });
  await expect(rule.getByRole('button', { name: '上移其他 / 未识别', exact: true })).toBeDisabled();
  expect(state.writes).toHaveLength(0);
  await page.getByRole('button', { name: '保存 1 项修改' }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  const write = state.writes.find((r) => r.path === '/api/cloud/settings')!;
  const body = write.body as { quality8_rules: { key: string; enabled: boolean }[] };
  expect(Object.keys(body)).toEqual(['quality8_rules']);
  expect(body.quality8_rules).toHaveLength(8);
  expect(body.quality8_rules.find((r) => r.key === 'resolution')?.enabled).toBe(false);
  expect(body.quality8_rules[6]?.key).toBe('size');
  expect(state.writes.some((r) => r.path === '/api/cover-strategy')).toBe(false);
});

test('a new log filter cancels the old read and ignores its late response', async ({ page }) => {
  await fixtures(page);
  let release!: () => void, started!: () => void;
  const pending = new Promise<void>((r) => {
      release = r;
    }),
    requested = new Promise<void>((r) => {
      started = r;
    });
  await page.route('**/api/logs?**', async (route) => {
    const query = new URL(route.request().url()).searchParams.get('query');
    if (!query) {
      started();
      await pending;
    }
    await fulfill(route, {
      status: 'success',
      logs: [
        {
          id: query ? 2 : 1,
          level: 'INFO',
          source: 'cloud',
          message: query ? 'MATCH-filtered' : 'OLD-unfiltered',
          ts: 'now',
        },
      ],
      cursor: query ? 2 : 1,
    }).catch(() => {});
  });
  await page.goto('/#/logs');
  await requested;
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  await page.getByLabel('关键词', { exact: true }).fill('MATCH');
  await page.getByRole('button', { name: '应用筛选', exact: true }).click();
  await expect(page.getByText('MATCH-filtered', { exact: true })).toBeVisible();
  release();
  await expect(page.getByText('OLD-unfiltered', { exact: true })).toHaveCount(0);
});

test('clear log screen retains the cursor and does not reload historical rows', async ({
  page,
}) => {
  await fixtures(page);
  const cursors: number[] = [];
  await page.route('**/api/logs?**', async (route) => {
    const after = Number(new URL(route.request().url()).searchParams.get('after'));
    cursors.push(after);
    await fulfill(route, {
      status: 'success',
      logs: after
        ? []
        : [{ id: 7, level: 'INFO', source: 'cloud', message: 'Historical-row', ts: 'now' }],
      cursor: 7,
    });
  });
  await page.goto('/#/logs');
  await expect(page.getByText('Historical-row', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  await page.getByRole('button', { name: '清空屏幕', exact: true }).click();
  await page.getByRole('button', { name: '立即更新', exact: true }).click();
  await expect.poll(() => cursors.length).toBe(2);
  expect(cursors).toEqual([0, 7]);
  await expect(page.getByText('Historical-row', { exact: true })).toHaveCount(0);
});

test('connection tests omit untouched secret drafts and submit a newly entered proxy password', async ({
  page,
}) => {
  await fixtures(page);
  let body: Record<string, unknown> = {};
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await fulfill(route, {
      status: 'success',
      config: {
        emby_host: 'http://localhost:8096',
        emby_key: 'saved****key',
        http_proxy_enabled: '1',
        http_proxy_url: 'http://127.0.0.1:8080',
        http_proxy_username: 'review-user',
        http_proxy_password: '********',
      },
      env_overridden: [],
    });
  });
  await page.route('**/api/config/test/proxy', async (route) => {
    body = route.request().postDataJSON();
    await fulfill(route, { status: 'success', message: 'Proxy-tested' });
  });
  await page.goto('/#/settings');
  const panel = page
    .locator('.settings-panel')
    .filter({ has: page.getByRole('heading', { name: 'HTTP 网络代理', exact: true }) });
  await panel.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(page.getByText('Proxy-tested', { exact: true })).toBeVisible();
  expect(body).not.toHaveProperty('http_proxy_password');
  expect(body).not.toHaveProperty('emby_key');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByLabel('代理密码', { exact: true }).fill('new-review-password');
  await panel.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(page.getByText('Proxy-tested', { exact: true })).toBeVisible();
  expect(body.http_proxy_password).toBe('new-review-password');
});
