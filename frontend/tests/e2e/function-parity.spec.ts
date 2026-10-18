import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('sidebar has ten primary entries and related controls remain inside their modules', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/organize/workspace');
  if (await page.getByRole('button', { name: '展开导航' }).isVisible())
    await page.getByRole('button', { name: '展开导航' }).click();
  const nav = page.getByRole('navigation', { name: '主导航' });
  await expect(nav.locator('.nav-direct')).toHaveCount(9);
  await expect(nav.locator('.nav-group-toggle')).toHaveCount(1);
  await expect(nav.getByRole('link', { name: '自定义命名模板', exact: true })).toHaveCount(0);
  if (await page.getByRole('button', { name: '关闭导航' }).last().isVisible())
    await page.getByRole('button', { name: '关闭导航' }).last().click();
  const module = page.getByRole('navigation', { name: '模块功能' });
  await module.getByRole('link', { name: '八维与洗版', exact: true }).click();
  await expect(page.getByRole('heading', { name: '整理八维与洗版', exact: true })).toBeVisible();
  await module.getByRole('link', { name: '需要处理的文件类型', exact: true }).click();
  await expect(page.getByLabel('最小视频体积（MB）', { exact: true })).toBeVisible();
  await expect(module.getByRole('link', { name: '需要处理的文件类型', exact: true })).toHaveClass(
    /current/,
  );
  expect(state.writes).toEqual([]);
});

test('offline deep link loads after account initialization and reloads for the selected account', async ({
  page,
}) => {
  await fixtures(page);
  const accounts: string[] = [];
  await page.route('**/api/cloud/resources/offline?**', (route) => {
    const id = new URL(route.request().url()).searchParams.get('account_id')!;
    accounts.push(id);
    return fulfill(route, {
      status: 'success',
      items: [{ name: `${id}的离线资源`, status: 'running' }],
      page_count: 1,
    });
  });
  await page.goto('/#/resources/receive?tab=offline&from=test');
  await expect(page.getByText('account-a的离线资源')).toBeVisible();
  await page.getByLabel('当前 115 账号').selectOption('account-b');
  await expect(page.getByText('account-b的离线资源')).toBeVisible();
  await expect(page.getByText('account-a的离线资源')).toHaveCount(0);
  expect(accounts).toContain('account-b');
  await page.evaluate(() => {
    location.hash = '/resources/receive';
  });
  await expect(page.getByRole('heading', { name: '添加资源链接', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '115 离线任务', exact: true })).toHaveCount(0);
});

test('function search retains nested naming, eight-dimension and share functions', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/dashboard');
  await page.getByRole('button', { name: '全局搜索功能' }).click();
  await page.getByLabel('搜索功能名称').fill('八维');
  await page
    .getByRole('dialog')
    .getByRole('link', { name: /八维洗版/ })
    .click();
  await expect(page.getByRole('heading', { name: '整理八维与洗版', exact: true })).toBeVisible();
  await page.goto('/#/resources');
  await page.getByLabel('筛选功能卡片').fill('免转存');
  await expect(page.locator('.tool-card')).toHaveCount(1);
  expect(state.writes).toEqual([]);
});

test('one-click intake submits saved directory revision and idempotency key only after confirmation', async ({
  page,
}) => {
  await fixtures(page);
  const writes: unknown[] = [];
  await page.route('**/api/cloud/resources/receive', (route) => {
    writes.push(route.request().postDataJSON());
    return fulfill(route, { status: 'success', job_id: 'accepted-intake' }, 202);
  });
  await page.route('**/api/cloud/jobs/accepted-intake', (route) =>
    fulfill(route, {
      status: 'success',
      job: { id: 'accepted-intake', kind: 'intake', state: 'queued' },
    }),
  );
  await page.goto('/#/resources/receive');
  await expect(page.getByLabel('已保存的接收目录')).toContainText('目录2');
  await page
    .getByLabel('资源内容')
    .fill('https://115.com/s/example\nmagnet:?xt=urn:btih:' + 'a'.repeat(40));
  await page.getByRole('button', { name: '一键提交到已保存目录' }).click();
  expect(writes).toEqual([]);
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toMatchObject({
    account_id: 'account-a',
    expected_revision: 3,
    organize: false,
    confirmed: true,
  });
  expect((writes[0] as { request_id: string }).request_id).toMatch(/^[a-f0-9]{32}$/);
  await expect(page.getByLabel('资源内容')).toHaveValue('');
  await expect(page.locator('.task-receipt')).toContainText('accepted-intake');
});

test('module directory changes only its selected role after revision-checked save', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/resources/receive');
  await page.locator('.module-config > summary').click();
  const field = page.locator('.directory-role-card .dirfield').first();
  await field.getByRole('button', { name: '选择目录', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: /影视归档/ })
    .click();
  await page.getByRole('button', { name: '使用当前目录', exact: true }).click();
  expect(state.writes).toEqual([]);
  await page.getByRole('button', { name: '保存本模块目录', exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  const body = state.writes[0]!.body as {
    directories: Record<string, string>;
    expected_revision: number;
  };
  expect(body).toEqual({ expected_revision: 3, directories: { transfer: '100' } });
});

test('technical fingerprints remain available without overwhelming normal results', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/cloud/resources/preview', (route) =>
    fulfill(route, {
      status: 'success',
      plan: {
        id: 'private-plan-id',
        reason: '作品识别完成',
        source_fingerprints: [{ sha1: 'A'.repeat(40), name: '影片.mkv' }],
        items: [{ name: '影片.mkv', kind: 'movie' }],
      },
    }),
  );
  await page.goto('/#/resources/receive');
  await page.getByLabel('资源内容').fill('https://115.com/s/example');
  await page.getByRole('button', { name: '识别资源并预览' }).click();
  await expect(page.getByText('作品识别完成')).toBeVisible();
  await expect(page.getByText('private-plan-id', { exact: true })).toBeHidden();
  const details = page.locator('.preview-panel').locator('details.technical-details').first();
  await details.locator('summary').first().click();
  await expect(page.getByText('private-plan-id', { exact: true })).toBeVisible();
});

test('naming and source-governance controls remain reachable inside organize', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/organize/naming');
  await expect(page.getByLabel('整理时重命名', { exact: true })).toBeVisible();
  await expect(page.getByLabel('使用 ffprobe 提取媒体信息', { exact: true })).toBeVisible();
  await expect(page.getByLabel('整理后清理空目录', { exact: true })).toBeVisible();
  await page
    .getByRole('navigation', { name: '模块功能' })
    .getByRole('link', { name: '删除与源文件处理', exact: true })
    .click();
  await expect(page.getByLabel('允许通过官方授权删除源文件', { exact: true })).toBeVisible();
  expect(state.writes).toEqual([]);
});

test('STRM generator loads its configured source without overwriting a manual selection on refresh', async ({
  page,
}) => {
  await fixtures(page);
  await page.goto('/#/media/strm');
  await expect(
    page.getByRole('textbox', { name: '手动同步的 115 来源目录 选择目录', exact: true }),
  ).toHaveValue('目录4');
  await expect(page.getByRole('button', { name: '核对并同步 STRM', exact: true })).toBeEnabled();
  await page
    .locator('.strm-sync-source')
    .getByRole('button', { name: '选择目录', exact: true })
    .click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: /影视归档/ })
    .click();
  await page.getByRole('button', { name: '使用当前目录', exact: true }).click();
  await page.getByRole('button', { name: '刷新并核实', exact: true }).click();
  await expect(
    page.getByRole('textbox', { name: '手动同步的 115 来源目录 选择目录', exact: true }),
  ).toHaveValue('根目录 / 影视归档');
});
