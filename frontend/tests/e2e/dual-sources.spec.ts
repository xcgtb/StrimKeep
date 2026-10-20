import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';
const sources = [
  {
    id: 'upstream_local',
    origin: 'upstream',
    label: '上游个人库',
    root: '/media/local',
    emby_root: '/emby/old/local',
    count: 12,
    governance: true,
  },
  {
    id: 'upstream_share',
    origin: 'upstream',
    label: '上游分享库',
    root: '/media/share',
    emby_root: '/emby/old/share',
    count: 8,
    governance: true,
  },
  {
    id: 'generated_local',
    origin: 'generated',
    label: '自建个人库',
    root: '/strm/local',
    emby_root: '/emby/new/local',
    count: 3,
    governance: false,
  },
  {
    id: 'generated_share',
    origin: 'generated',
    label: '自建分享库',
    root: '/strm/share',
    emby_root: '',
    count: null,
    message: '尚未配置此来源的 Emby 路径',
    governance: false,
  },
];

test('overview keeps both origins visible without reporting an unverified source as zero', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/library/sources', (route) =>
    fulfill(route, { status: 'success', items: sources }),
  );
  await page.goto('/#/dashboard');
  const panel = page.getByRole('region', { name: '媒体来源', exact: true });
  await expect(panel.getByText('上游个人库', { exact: true })).toBeVisible();
  await expect(panel.getByText('自建个人库', { exact: true })).toBeVisible();
  await expect(panel.getByText('待核验', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '展开来源路径', exact: true }).click();
  await expect(panel.getByText('尚未配置此来源的 Emby 路径', { exact: true })).toBeVisible();
  await expect(panel.getByText('/media/local', { exact: true })).toBeVisible();
  await expect(panel.getByText('/strm/local', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('link', { name: '配置 Emby 路径 →' }).click();
  await expect(page.getByRole('heading', { name: '连接与设置', exact: true })).toBeVisible();
});

test('connection settings expose separate upstream and generated Emby roots', async ({ page }) => {
  await fixtures(page);
  await page.goto('/#/settings');
  // Service groups use a native details element; open only the relevant one.
  const details = page
    .locator('details')
    .filter({ has: page.getByText('Emby 媒体服务', { exact: true }) });
  if (await details.count()) await details.locator('summary').click();
  await expect(page.getByLabel('Emby 中的上游个人库路径', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Emby 中的上游分享库路径', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Emby 中的自建个人库路径', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Emby 中的自建分享库路径', { exact: true })).toBeVisible();
});

test('mapping posters show upstream and generated origins separately', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/emby/library/page*', (route) =>
    fulfill(route, {
      status: 'success',
      media: 'series',
      total: 1,
      has_more: false,
      next_offset: 1,
      rows: [
        {
          id: 'dual-movie',
          name: '双来源剧集',
          year: 2026,
          tmdb_id: '44',
          in_local: true,
          in_share: true,
          source_labels: ['上游个人库', '自建分享库'],
          have_eps: 2,
          local_eps: 1,
          share_eps: 2,
          tmdb_info: { match_status: 'aligned', declared_total: 2 },
        },
      ],
    }),
  );
  await page.goto('/#/media/library');
  const tags = page.locator('.library-tags');
  await expect(tags.getByText('上游个人库', { exact: true })).toBeVisible();
  await expect(tags.getByText('自建分享库', { exact: true })).toBeVisible();
});
