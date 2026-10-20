import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

const channel = {
  id: 'rule-a',
  revision: 1,
  name: '我的影视频道',
  address: '@moviechannel',
  mode: 'public_web',
  action: 'transfer',
  target_cid: '100',
  enabled: false,
  auto_transfer: true,
  organize: false,
  history_limit: 20,
  poll_seconds: 300,
  include: [],
  exclude: [],
  regex: [],
  tmdb_ids: [],
  kinds: [],
  countries: [],
  genres: [],
  rule_cards: [],
};

test('channel displays the shared destinations and explicitly saves post-transfer organization', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.route('**/api/cloud/accounts/*/channels', async (route) => {
    if (route.request().method() === 'POST') {
      state.writes.push({
        path: new URL(route.request().url()).pathname,
        body: route.request().postDataJSON(),
      });
      return fulfill(route, { status: 'success', channel });
    }
    return fulfill(route, { status: 'success', items: [channel] });
  });
  await page.route('**/api/cloud/accounts/*/pipeline', (route) =>
    fulfill(route, {
      status: 'success',
      directories: { target: { id: '101', name: '影视归档' } },
      emby_mappings: { items: [] },
      checks: [],
    }),
  );
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '编辑规则', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog
    .getByLabel('转存后自动识别、命名、归档并按已保存开关生成 STRM', { exact: true })
    .check();
  await expect(dialog.getByText('影视归档', { exact: true })).toBeVisible();
  await expect(dialog.getByText(/匹配资源 → 转存 → 自动识别/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await dialog.getByRole('button', { name: '保存频道规则', exact: true }).click();
  await expect
    .poll(() => state.writes.find((r) => r.path.endsWith('/channels'))?.body)
    .toMatchObject({ organize: true, target_cid: '100', enabled: false });
});

test('cursor reset explains skipped history and requires a separate confirmation', async ({
  page,
}) => {
  await fixtures(page);
  const applied: unknown[] = [];
  await page.route('**/api/cloud/accounts/*/channels', (route) =>
    fulfill(route, {
      status: 'success',
      items: [{ ...channel, reader_status: '历史窗口缺失，请人工检查' }],
    }),
  );
  await page.route('**/channels/rule-a/position/preview', (route) =>
    fulfill(route, {
      status: 'success',
      token: 'position-preview',
      previous_cursor: 10,
      latest_cursor: 30,
      message: '确认后将略过尚未接收的历史；监控保持暂停。',
    }),
  );
  await page.route('**/channels/rule-a/position/apply', (route) => {
    applied.push(route.request().postDataJSON());
    return fulfill(route, { status: 'success', message: '读取位置已更新，频道仍暂停' });
  });
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '重新建立读取位置', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/原位置 #10/)).toBeVisible();
  expect(applied).toHaveLength(0);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(applied).toHaveLength(0);
  await page.getByRole('button', { name: '重新建立读取位置', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '确认略过历史并更新位置', exact: true }).click();
  await expect.poll(() => applied.length).toBe(1);
  expect(applied[0]).toEqual({ token: 'position-preview', confirmed: true });
  await expect(page.getByRole('button', { name: '启用监控', exact: true })).toBeVisible();
});
