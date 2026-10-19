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

test('external forwarding asks for the recipient and explains the session requirement', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '万能转发', exact: true }).click();
  await page.getByRole('button', { name: '添加转发规则', exact: true }).click();
  await page.getByLabel('频道名称', { exact: true }).fill('外部转发');
  await page.getByLabel('频道地址 / @名称 / -100 ID', { exact: true }).fill('@moviechannel');
  await expect(page.getByLabel('目标机器人', { exact: true })).toHaveCount(0);
  await page.getByLabel('接收位置', { exact: true }).selectOption('external');
  await page.getByLabel('目标机器人', { exact: true }).fill('@targetrobot');
  await page.getByLabel('转发命令头', { exact: true }).fill('/custom');
  await expect(page.getByText(/需要先登录用户会话/, { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '保存频道规则', exact: true }).click();
  await expect.poll(() => state.writes.filter((r) => r.path.endsWith('/channels')).length).toBe(1);
  expect(state.writes.find((r) => r.path.endsWith('/channels'))?.body).toMatchObject({
    forward_target: '@targetrobot',
    command_prefix: '/custom',
    enabled: false,
  });
  await expect(
    page.getByText('规则已保存为暂停状态。先读取并检查，再点启用。', { exact: true }),
  ).toBeVisible();
});

test('channel inspection displays messages and reasons without enabling or sending', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.route('**/api/cloud/accounts/*/channels', (route) =>
    fulfill(route, {
      status: 'success',
      items: [
        {
          id: 'rule-a',
          revision: 1,
          name: '示例频道',
          address: '@moviechannel',
          action: 'share_strm',
          mode: 'public_web',
          enabled: false,
          auto_transfer: true,
        },
      ],
    }),
  );
  await page.route('**/channels/rule-a/inspect', (route) =>
    fulfill(route, {
      status: 'success',
      note: '未改变读取位置，也没有触发处理',
      checks: [{ label: '读取频道', ok: true, detail: '成功读取最近消息' }],
      messages: [
        {
          message_id: 12,
          text: '资源链接位于按钮',
          matched: false,
          reason: '命中排除关键词',
          would_do: '跳过：命中排除关键词',
        },
      ],
    }),
  );
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '读取并检查', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '读取与处理检查', exact: true });
  await expect(dialog.getByText('成功读取最近消息', { exact: true })).toBeVisible();
  await expect(dialog.getByText('命中排除关键词', { exact: true })).toBeVisible();
  await expect(dialog.getByText('资源链接位于按钮', { exact: true })).toBeVisible();
  expect(state.writes.filter((r) => r.path.endsWith('/channels'))).toHaveLength(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('message filter preview shows the actual decision directly in the dialog', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/cloud/accounts/*/channels', (route) =>
    fulfill(route, {
      status: 'success',
      items: [
        {
          id: 'rule-a',
          revision: 1,
          name: '示例频道',
          action: 'transfer',
          mode: 'public_web',
          enabled: false,
        },
      ],
    }),
  );
  await page.route('**/channels/rule-a/test', (route) =>
    fulfill(route, {
      status: 'success',
      matched: false,
      reason: '没有找到支持的资源链接',
      would_do: '跳过：没有找到支持的资源链接',
    }),
  );
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '测试筛选', exact: true }).click();
  await page.getByLabel('示例消息', { exact: true }).fill('普通公告');
  await page.getByRole('button', { name: '测试匹配与原因', exact: true }).click();
  await expect(page.getByRole('heading', { name: '消息不会处理', exact: true })).toBeVisible();
  await expect(page.getByText('没有找到支持的资源链接', { exact: true })).toBeVisible();
});

test('pause sends only the versioned switch rather than revalidating all settings', async ({
  page,
}) => {
  const state = await fixtures(page);
  let enabled = true;
  await page.route('**/api/cloud/accounts/*/channels', (route) =>
    fulfill(route, {
      status: 'success',
      items: [
        {
          id: 'rule-a',
          revision: enabled ? 1 : 2,
          name: '示例频道',
          address: '@moviechannel',
          action: 'transfer',
          mode: 'public_web',
          enabled,
        },
      ],
    }),
  );
  await page.route('**/channels/rule-a/enabled', (route) => {
    state.writes.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON(),
    });
    enabled = false;
    return fulfill(route, { status: 'success', channel: { enabled: false, revision: 2 } });
  });
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '暂停监控', exact: true }).click();
  await expect(page.getByRole('button', { name: '启用监控', exact: true })).toBeVisible();
  expect(state.writes.find((r) => r.path.endsWith('/enabled'))?.body).toEqual({
    enabled: false,
    revision: 1,
  });
  expect(state.writes.filter((r) => r.path.endsWith('/channels'))).toHaveLength(0);
});
