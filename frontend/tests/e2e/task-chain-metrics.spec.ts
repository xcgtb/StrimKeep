import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('task chain navigates between related receipts without leaving stale detail', async ({
  page,
}) => {
  await fixtures(page);
  const chain = {
    status: 'success',
    root_id: 'job-a',
    state: 'waiting',
    jobs: [
      { id: 'job-a', label: '资源接收', state: 'success', message: '资源请求已提交' },
      { id: 'job-b', label: '延时发布', state: 'success', message: '等待入库确认' },
    ],
    ingestion: { confirmed: 2, pending: 1 },
    notifications: { verified: 1 },
  };
  await page.route('**/api/cloud/jobs/*/chain', (r) => fulfill(r, chain));
  await page.route('**/api/cloud/jobs/job-b', (r) =>
    fulfill(r, {
      status: 'success',
      job: { id: 'job-b', kind: 'publish', state: 'success', message: '等待入库确认' },
    }),
  );
  await page.goto('/#/tasks?job=job-a');
  const flow = page.getByRole('region', { name: '完整任务链' });
  await expect(flow.getByText('2 已确认')).toBeVisible();
  await flow.getByText(/关联任务与执行回执/).click();
  await flow.getByRole('link', { name: '查看', exact: true }).click();
  await expect(page).toHaveURL(/job=job-b/);
  await expect(
    page.getByRole('dialog').getByText('等待入库确认', { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: '核对恢复范围' })).toHaveCount(0);
});

test('recovery requires a fresh preview and explicit confirmation', async ({ page }) => {
  await fixtures(page);
  const posts: { path: string; body: unknown }[] = [];
  await page.route('**/api/cloud/jobs/job-a/recovery/*', (r) => {
    posts.push({
      path: new URL(r.request().url()).pathname,
      body: r.request().postData() ? r.request().postDataJSON() : null,
    });
    return fulfill(
      r,
      r.request().url().endsWith('/preview')
        ? {
            status: 'success',
            preview_id: 'fresh-preview',
            items: [{ name: '影片.mkv', target: '电影/影片', followers: 1 }],
            blocked: [],
            message: '只继续剩余文件',
          }
        : { status: 'success', job_id: 'job-b', message: '后续任务已存在' },
    );
  });
  await page.goto('/#/tasks?job=job-a');
  await page.getByRole('button', { name: '核对恢复范围', exact: true }).click();
  await expect(
    page
      .getByRole('region', { name: '核实与恢复' })
      .locator('.recovery-files strong')
      .filter({ hasText: '影片.mkv' }),
  ).toBeVisible();
  expect(posts).toHaveLength(1);
  await page.getByRole('button', { name: '确认继续以上项目' }).click();
  const confirmation = page.getByRole('dialog').last();
  await expect(confirmation.getByRole('heading', { name: '继续已核实项目' })).toBeVisible();
  await confirmation.getByRole('checkbox').check();
  await confirmation.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect(page.getByRole('link', { name: '查看后续任务', exact: true })).toBeVisible();
  expect(posts[1]?.body).toEqual({ preview_id: 'fresh-preview', confirmed: true });
});

test('playback metrics use server facts, change range and persist the collection switch', async ({
  page,
}) => {
  await fixtures(page);
  let enabled = false;
  const ranges: string[] = [];
  const data = () => ({
    status: 'success',
    configured: true,
    enabled,
    seconds: 8100,
    plays: 12,
    viewers: 3,
    last_poll: 1792330000,
    collected_since: 1792300000,
    source: 'Emby 播放会话 · 每15秒观测',
    coverage: '仅统计连续观测到的有效播放段',
    trend: [{ day: '2026-10-18', seconds: 8100, plays: 12, viewers: 3 }],
    rankings: [{ item_id: 'm1', title: '真实播放影片', seconds: 3600 }],
    users: [{ user_id: 'u1', name: '家人', seconds: 3600 }],
    active: [],
  });
  await page.route('**/api/overview/playback/metrics?**', (r) => {
    ranges.push(new URL(r.request().url()).searchParams.get('days') || '');
    return fulfill(r, data());
  });
  await page.route('**/api/overview/playback/collection', (r) => {
    enabled = r.request().postDataJSON().enabled;
    return fulfill(r, data());
  });
  await page.goto('/#/dashboard');
  const card = page.getByRole('region', { name: '真实播放统计' });
  await expect(card.getByText('真实播放影片', { exact: true })).toBeVisible();
  await expect(
    card.locator('.playback-totals strong').filter({ hasText: '2 小时 15 分钟' }),
  ).toBeVisible();
  await card.getByRole('button', { name: '启用采集', exact: true }).click();
  await expect(card.getByRole('button', { name: '暂停采集' })).toBeVisible();
  await card.getByLabel('播放统计范围').selectOption('30');
  await expect.poll(() => ranges.at(-1)).toBe('30');
  await page.reload();
  await expect(card.getByRole('button', { name: '暂停采集' })).toBeVisible();
});

test('share route separates source, NAS output and Emby mapping', async ({ page }) => {
  await fixtures(page);
  await page.goto('/#/resources/share');
  const route = page.getByRole('region', { name: '本次资源去向' });
  await expect(route.getByText('115 分享来源', { exact: true })).toBeVisible();
  await expect(route.getByText('NAS 分享库 STRM', { exact: true })).toBeVisible();
  await expect(route.getByText('Emby 可见路径', { exact: true })).toBeVisible();
  await expect(route.getByText('保留分享源，不占个人网盘转存空间。')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
});

test('uncertain recovery submit is reconciled without resending the write', async ({ page }) => {
  await fixtures(page);
  let writes = 0,
    child = false;
  await page.route('**/api/cloud/jobs/job-a/recovery/preview', (r) =>
    fulfill(r, {
      status: 'success',
      preview_id: 'p1',
      items: [{ name: '影片.mkv', target: '电影', followers: 0 }],
      blocked: [],
    }),
  );
  await page.route('**/api/cloud/jobs/job-a/recovery/apply', (r) => {
    writes++;
    child = true;
    return fulfill(r, { detail: '连接断开，需核实' }, 503);
  });
  await page.route('**/api/cloud/jobs/job-a/chain', (r) =>
    fulfill(r, {
      status: 'success',
      root_id: 'job-a',
      state: 'running',
      jobs: [
        {
          id: 'job-a',
          resolved_by: child ? 'job-b' : null,
          label: '整理归档',
          state: 'needs_review',
        },
      ],
      ingestion: {},
      notifications: {},
    }),
  );
  await page.goto('/#/tasks?job=job-a');
  await page.getByRole('button', { name: '核对恢复范围', exact: true }).click();
  await page.getByRole('button', { name: '确认继续以上项目' }).click();
  const confirm = page.getByRole('dialog').last();
  await confirm.getByRole('checkbox').check();
  await confirm.getByRole('button', { name: '确认执行', exact: true }).click();
  await page.getByRole('button', { name: '只读核实原任务', exact: true }).click();
  await expect(page.getByRole('link', { name: '查看后续任务', exact: true })).toBeVisible();
  expect(writes).toBe(1);
});

test('uncertain collection toggle can be read back without another write', async ({ page }) => {
  await fixtures(page);
  let enabled = false,
    writes = 0;
  const data = () => ({
    status: 'success',
    configured: true,
    enabled,
    seconds: 0,
    plays: 0,
    viewers: 0,
    trend: [],
    rankings: [],
    users: [],
    active: [],
  });
  await page.route('**/api/overview/playback/metrics?**', (r) => fulfill(r, data()));
  await page.route('**/api/overview/playback/collection', (r) => {
    enabled = true;
    writes++;
    return fulfill(r, { detail: '服务回执未知' }, 503);
  });
  await page.goto('/#/dashboard');
  const card = page.getByRole('region', { name: '真实播放统计' });
  await card.getByRole('button', { name: '启用采集' }).click();
  await card.getByRole('button', { name: '重新读取采集状态' }).click();
  await expect(card.getByRole('button', { name: '暂停采集' })).toBeEnabled();
  expect(writes).toBe(1);
});
