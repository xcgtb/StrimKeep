import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('universal forward saves explicit routing without a transfer directory', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '万能转发', exact: true }).click();
  await page.getByRole('button', { name: '添加转发规则', exact: true }).click();
  await page.getByLabel('频道名称', { exact: true }).fill('转发影视频道');
  await page.getByLabel('频道地址 / @名称 / -100 ID', { exact: true }).fill('@moviechannel');
  await page.getByLabel('转发命令头', { exact: true }).fill('/create_115share_strm');
  await expect(page.getByText('频道保存目录', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '保存频道规则', exact: true })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: '保存频道规则', exact: true }).click();
  await expect.poll(() => state.writes.filter((r) => r.path.endsWith('/channels')).length).toBe(1);
  expect(state.writes.find((r) => r.path.endsWith('/channels'))?.body).toMatchObject({
    action: 'forward',
    command_prefix: '/create_115share_strm',
    forward_target: 'self',
    target_cid: '',
    mode: 'public_web',
    history_limit: 20,
  });
});

test('history explains failed stage and downstream transfer result', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/cloud/accounts/*/inbox?**', (route) =>
    fulfill(route, {
      status: 'success',
      items: [
        {
          id: 'inbox-fixture',
          source: 'channel',
          state: 'processed',
          job: { message: '接收请求已提交' },
          result: {
            channel: '影视频道',
            errors: [{ stage_label: '读取分享', reason: '115 分享已取消（错误码 4100010）' }],
            child_errors: [{ stage_label: '核验转存结果', reason: '网络读取失败，请先核实结果' }],
          },
        },
      ],
    }),
  );
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '接收历史', exact: true }).click();
  await expect(
    page.getByText('读取分享：115 分享已取消（错误码 4100010）', { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText('后续任务 · 核验转存结果：网络读取失败，请先核实结果', { exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('running channel tasks expose safe stop and pause control', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/cloud/jobs/job-a', (route) =>
    fulfill(route, {
      status: 'success',
      job: {
        id: 'job-a',
        kind: 'channel',
        state: 'running',
        message: '准备转发命令消息',
        result: {},
        steps: [],
      },
    }),
  );
  await page.goto('/#/tasks?job=job-a');
  await expect(page.getByRole('button', { name: '安全停止并暂停频道', exact: true })).toBeVisible();
});
