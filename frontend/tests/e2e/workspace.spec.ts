import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('login rejects bad passwords, then restores a deep link and never stores credentials', async ({
  page,
}) => {
  await fixtures(page, false);
  await page.goto('/#/media/library');
  await expect(page.getByRole('heading', { name: '登录 StrimKeep' })).toBeVisible();
  await page.getByLabel('管理密码').fill('wrong');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('alert')).toHaveText('用户名或密码错误');
  await page.getByLabel('管理密码').fill('correct');
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByRole('heading', { name: '片库映射', exact: true })).toBeVisible();
  expect(
    await page.evaluate(() => ({
      local: JSON.stringify(localStorage),
      session: JSON.stringify(sessionStorage),
      cookie: document.cookie,
    })),
  ).toEqual({ local: '{}', session: '{}', cookie: '' });
});

test('overview shows real values, routes posters to explore and survives refresh', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/dashboard');
  await expect(page.getByText('140,845', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看星际穿越详情' }).first().click();
  await expect(page.getByRole('heading', { name: '影视探索', exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '搜索影视作品' })).toHaveValue('星际穿越');
  await page.reload();
  await expect(page.getByRole('textbox', { name: '搜索影视作品' })).toHaveValue('星际穿越');
  expect(state.calls.some((s) => s.includes('/api/emby/library?'))).toBe(false);
});

test('theme and responsive navigation work without horizontal overflow', async ({ page }) => {
  await fixtures(page);
  await page.goto('/#/dashboard');
  await page.getByRole('button', { name: '切换深色主题' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  if (await page.getByRole('button', { name: '展开导航' }).isVisible())
    await page.getByRole('button', { name: '展开导航' }).click();
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '追更订阅' })
    .click();
  await expect(page.getByRole('heading', { name: '追更订阅', exact: true })).toBeVisible();
});

test('explore loads another page and manages subscriptions in details', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/media/explore');
  await expect(page.locator('.poster-card')).toHaveCount(12);
  await expect(page.locator('.poster-card').first().locator('.poster-meta span')).toHaveText([
    '2026',
    '剧集',
  ]);
  await expect(page.locator('.poster-card').nth(1).locator('.poster-meta span')).toHaveText([
    '2025',
    '电影',
  ]);
  await page.getByRole('button', { name: '加载更多作品' }).click();
  await expect(page.locator('.poster-card')).toHaveCount(13);
  await expect(page.locator('.subscribe-button')).toHaveCount(0);
  await page.locator('.poster-card').first().locator('.poster-open').click();
  const detail = page.getByRole('dialog');
  await detail.getByRole('button', { name: '取消追更', exact: true }).click();
  await expect(detail.getByRole('button', { name: '添加追更订阅', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(state.writes.filter((c) => c.path === '/api/subscriptions')).toHaveLength(1);
});

test('library selection is separate from details and uses bounded pagination', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/media/library');
  await page.getByRole('button', { name: '多选清理' }).click();
  await page.getByRole('button', { name: '本地库（联动源文件）', exact: true }).click();
  await page.getByRole('button', { name: '选择星际穿越', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('已选择 1 部')).toBeVisible();
  await page.getByRole('button', { name: '查看所选状态' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '关闭详情' }).click();
  await page.getByRole('button', { name: '退出多选' }).click();
  await page.getByRole('button', { name: '查看星际穿越详情' }).click();
  await expect(page.getByRole('dialog')).toContainText('缺 6 集');
  await expect(page.getByRole('dialog')).toContainText('TMDB 已播 24 集');
  expect(state.calls.some((c) => c === 'GET /api/emby/library')).toBe(false);
});

test('diagnostics show file reasons, suggestions and escape untrusted content', async ({
  page,
}) => {
  await fixtures(page);
  await page.goto('/#/tasks');
  await page.getByRole('button', { name: /部分文件需要核对/ }).click();
  await expect(page.getByRole('dialog')).toContainText('无法识别季集');
  await expect(page.getByRole('dialog')).toContainText('核对片名、年份与季集后重新预览。');
  await expect(page.getByRole('dialog').locator('img')).toHaveCount(0);
});

test('115 file browser shows files inside folders and breadcrumbs navigate back', async ({
  page,
}) => {
  await fixtures(page);
  await page.goto('/#/resources/files');
  await page.getByRole('button', { name: '影视归档' }).click();
  await expect(page.getByText('影片.mkv', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '返回上级目录' }).click();
  await expect(page.getByText('测试电影.mkv', { exact: true })).toBeVisible();
});

test('switching accounts ignores a late response from the previous account', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/cloud/files?**', async (route) => {
    const account = new URL(route.request().url()).searchParams.get('account_id');
    if (account === 'account-a') await new Promise((r) => setTimeout(r, 600));
    await fulfill(route, {
      status: 'success',
      items: [
        {
          id: account === 'account-a' ? '1' : '2',
          name: account === 'account-a' ? '旧账号文件' : '新账号文件',
          is_dir: false,
        },
      ],
      total: 1,
      has_more: false,
    });
  });
  await page.goto('/#/resources/files');
  await expect(page.getByRole('combobox', { name: '选择网盘账号' })).toBeVisible();
  await page.getByRole('combobox', { name: '选择网盘账号' }).selectOption('account-b');
  await expect(page.getByText('新账号文件', { exact: true })).toBeVisible();
  await page.waitForTimeout(700);
  await expect(page.getByText('旧账号文件', { exact: true })).toHaveCount(0);
});

test('editing settings never writes until Save and only submits changed fields', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/settings');
  await page.getByLabel('服务地址', { exact: true }).fill('http://192.168.1.22:8096');
  expect(state.writes).toHaveLength(0);
  await expect(page.getByLabel('API Key', { exact: true })).toHaveAttribute('type', 'password');
  await page.getByRole('button', { name: '保存 1 项更改' }).click();
  await expect(page.getByRole('status')).toContainText('连接设置已保存');
  expect(state.writes[0]).toEqual({
    path: '/api/config',
    body: { emby_host: 'http://192.168.1.22:8096' },
  });
});

test('a settings 502 locks repeated submission until an explicit read', async ({ page }) => {
  const state = await fixtures(page);
  await page.route('**/api/config', async (route) =>
    route.request().method() === 'POST'
      ? fulfill(route, { detail: '网关暂不可用' }, 502)
      : route.fallback(),
  );
  await page.goto('/#/settings');
  await page.getByLabel('服务地址', { exact: true }).fill('http://changed:8096');
  await page.getByRole('button', { name: '保存 1 项更改' }).click();
  await expect(page.getByRole('button', { name: '重新读取已保存配置' })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存 1 项更改' })).toBeDisabled();
  expect(state.writes).toHaveLength(0);
});

test('expired sessions return to login', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/dashboard');
  await expect(page.getByText('140,845', { exact: true })).toBeVisible();
  state.authenticated = false;
  await page.getByRole('button', { name: '刷新总览' }).click();
  await expect(page.getByRole('heading', { name: '登录 StrimKeep' })).toBeVisible();
});

test('API failures show a retry action rather than fabricated data', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/emby/library/page?**', (route) =>
    fulfill(route, { detail: 'Emby 暂时不可用' }, 503),
  );
  await page.goto('/#/media/library');
  await expect(page.getByRole('alert')).toContainText('Emby 暂时不可用');
  await expect(page.getByRole('button', { name: '重新加载' })).toBeVisible();
  await expect(page.locator('.poster-card')).toHaveCount(0);
});

test('resource search opens the matching workspace without any write', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/resources');
  await page.getByRole('textbox', { name: '筛选功能卡片' }).fill('免转存');
  await expect(page.locator('.tool-card')).toHaveCount(1);
  await page.locator('.tool-card').click();
  await expect(
    page.getByRole('heading', { name: '分享 STRM 生成与整理', exact: true }),
  ).toBeVisible();
  await expect(page.locator('.share-path-banner')).toContainText('share');
  expect(state.writes).toHaveLength(0);
});

test('settings deep link opens the requested naming section', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/settings/cloud?section=naming');
  await expect(page.getByRole('heading', { name: '识别与命名', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '识别与命名', exact: true })).toBeVisible();
  expect(state.writes).toHaveLength(0);
});

test('mobile shortcut dock opens resources and the full navigation', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'Mobile shortcut navigation');
  await fixtures(page);
  await page.goto('/#/dashboard');
  const dock = page.getByRole('navigation', { name: '快捷导航' });
  await dock.getByRole('link', { name: '转存', exact: true }).click();
  await expect(page.getByRole('heading', { name: '链接与离线转存', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '展开导航' }).click();
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '分享 STRM 生成与整理', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: '分享 STRM 生成与整理', exact: true }),
  ).toBeVisible();
});

test('compact tablet navigation stays above its backdrop', async ({ page }) => {
  await page.setViewportSize({ width: 680, height: 900 });
  await fixtures(page);
  await page.goto('/#/resources');
  await page.getByRole('button', { name: '展开导航' }).click();
  await page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link', { name: '影视整理', exact: true })
    .click();
  await expect(page.getByRole('heading', { name: '媒体整理', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('naming variable guide inserts into the selected real backend settings draft', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/organize/naming');
  await expect(page.getByRole('heading', { name: '模板变量词典 · 中文解释与示例' })).toBeVisible();
  const input = page.getByLabel('搜索模板变量');
  await input.fill('年份');
  await expect(page.getByText('上映／首播年份')).toBeVisible();
  expect(state.writes).toHaveLength(0);
});

test('quality rule presets retain separate eight-dimensional settings and source-size dimension', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/organize/quality');
  await expect(page.getByRole('button', { name: '恢复八维默认草稿' })).toBeVisible();
  await page.getByRole('button', { name: '恢复八维默认草稿' }).click();
  await expect(page.getByText('真实源文件大小')).toBeVisible();
  expect(state.writes).toHaveLength(0);
});
