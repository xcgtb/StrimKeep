import { test, expect, type Page } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

const works = [
  {
    cid: '11',
    name: '电影甲 (2024) [tmdb-42]',
    title: '电影甲',
    year: 2024,
    kind: 'movie',
    tmdb_id: '42',
    path: '全部文件/电影/电影甲',
    video_count: 1,
    size: 4000000000,
    metadata_verified: false,
    files: [{ id: '12', name: 'Movie.mkv', size: 4000000000 }],
    episodes: [],
  },
  {
    cid: '14',
    name: '剧集乙 {tmdb-42}',
    title: '剧集乙',
    kind: 'tv',
    tmdb_id: '42',
    path: '全部文件/剧集/剧集乙',
    video_count: 3,
    episode_count: 4,
    unrecognized_episodes: 0,
    metadata_verified: true,
    poster: '/api/tmdb/poster/0.jpg',
    files: [],
    episodes: [
      [0, 1],
      [1, 1],
      [1, 2],
      [1, 3],
    ],
  },
];
async function inventoryFixture(page: Page) {
  const state = await fixtures(page);
  let scanned = true;
  const scans: unknown[] = [];
  await page.route('**/api/cloud/accounts/*/inventory**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/scan')) {
      scans.push(route.request().postDataJSON());
      scanned = true;
      return fulfill(route, { status: 'success', job_id: 'stock-job' }, 202);
    }
    const items = url.pathname.includes('account-b')
      ? []
      : works.filter(
          (row) =>
            (!url.searchParams.get('kind') || row.kind === url.searchParams.get('kind')) &&
            (!url.searchParams.get('keyword') ||
              row.title.includes(url.searchParams.get('keyword')!)),
        );
    return fulfill(route, {
      status: 'success',
      has_snapshot: scanned,
      items,
      total: items.length,
      counts: { all: 2, movie: 1, tv: 1, '': 0 },
      summary: { video_count: 4 },
      updated: 1792120000,
    });
  });
  await page.route('**/api/cloud/jobs/stock-job', (r) =>
    fulfill(r, {
      status: 'success',
      job: { id: 'stock-job', state: 'success', kind: 'inventory', message: '任务完成' },
    }),
  );
  return { state, scans };
}
async function directories(page: Page) {
  await page.route('**/api/cloud/accounts/*/directories', (r) =>
    fulfill(r, {
      status: 'success',
      revision: 2,
      directories: {
        source: { id: '1', name: '定时来源' },
        target: { id: '2', name: '归档' },
        strm: { id: '3', name: '定时STRM来源' },
      },
    }),
  );
}
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

test('inventory shows observed specials, filters and opens the actual cloud folder', async ({
  page,
}) => {
  const { state, scans } = await inventoryFixture(page);
  await page.goto('/#/resources/files?view=inventory');
  await expect(page.getByRole('heading', { name: '盘内媒体库存' })).toBeVisible();
  await page.screenshot({
    path: `test-results/inventory-${test.info().project.name}.png`,
    fullPage: true,
  });
  await page.getByRole('button', { name: '查看剧集乙详情' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('S00E01');
  await expect(dialog).toContainText('不据此判定整剧完整');
  await dialog.getByRole('button', { name: '关闭' }).click();
  await page.getByRole('button', { name: '电影 1', exact: true }).click();
  await expect(page.getByRole('button', { name: '查看剧集乙详情' })).toHaveCount(0);
  await page.getByRole('button', { name: '查看电影甲详情' }).click();
  await dialog.getByRole('button', { name: '浏览网盘目录' }).click();
  await expect(page.getByRole('navigation', { name: '文件目录' })).toContainText('电影甲');
  expect(state.calls.some((s) => s === 'GET /api/cloud/files')).toBe(true);
  expect(scans).toHaveLength(0);
  expect(state.writes).toHaveLength(0);
  await noOverflow(page);
});

test('inventory scan is explicit and switching accounts clears old works', async ({ page }) => {
  const { scans } = await inventoryFixture(page);
  await page.goto('/#/resources/files?view=inventory');
  await page.getByRole('button', { name: '扫描盘内库存', exact: true }).click();
  await expect.poll(() => scans.length).toBe(1);
  expect(scans[0]).toEqual({ root_cid: '0', kind_hint: 'auto', enrich: true });
  await expect(page.getByText('任务完成', { exact: true }).first()).toBeVisible();
  await page.getByLabel('选择网盘账号').selectOption('account-b');
  await expect(page.getByRole('button', { name: '查看电影甲详情' })).toHaveCount(0);
  await noOverflow(page);
});

for (const [path, label] of [
  ['/organize/workspace', '待整理目录'],
  ['/media/strm', '115 来源目录'],
]) {
  test(`inventory source loads into ${path} without saving or executing`, async ({ page }) => {
    const { state } = await inventoryFixture(page);
    await directories(page);
    await page.goto(
      `/#${path}?account=account-b&source_cid=14&source_name=${encodeURIComponent('剧集乙')}`,
    );
    await expect(page.getByText(/已带入盘内库存目录/)).toBeVisible();
    if (path === '/organize/workspace') {
      await expect(page.getByLabel(label!)).toHaveValue('剧集乙');
    } else {
      await expect(page.getByText('本次手动同步来源：剧集乙')).toBeVisible();
    }
    await expect(page.getByLabel('当前 115 账号')).toHaveValue('account-b');
    expect(state.writes).toHaveLength(0);
    await noOverflow(page);
  });
}

test('organize preview compares names and separates skipped and unidentified files', async ({
  page,
}) => {
  await fixtures(page);
  await directories(page);
  await page.route('**/api/cloud/plans', (r) =>
    fulfill(r, {
      status: 'success',
      plan: {
        id: 'p1',
        expires_in: 900,
        items: [
          {
            source: { id: '12', name: 'raw.S00E01.mkv' },
            media: {
              title: '剧集乙',
              kind: 'tv',
              tmdb_id: '42',
              season: 0,
              episode: 1,
              directory: '剧集/剧集乙/Season00',
              name: '剧集乙.S00E01.mkv',
            },
          },
        ],
        skipped: [{ file_id: '13', name: 'old.mkv', reason: '保留已有版本' }],
        unknown_items: [{ source: { id: '15', name: 'unknown.mkv' }, reason: '季集不明确' }],
      },
    }),
  );
  await page.goto('/#/organize/workspace');
  await page.getByRole('button', { name: '扫描并预览整理' }).click();
  await expect(page.getByText('原文件', { exact: true })).toHaveCount(3);
  await expect(page.getByText('整理后文件名', { exact: true })).toBeVisible();
  await expect(page.getByText('剧集乙.S00E01.mkv', { exact: true })).toBeVisible();
  await page.screenshot({
    path: `test-results/organize-preview-${test.info().project.name}.png`,
    fullPage: true,
  });
  await page.getByLabel('筛选整理结果').selectOption('unknown');
  await expect(page.getByText('unknown.mkv', { exact: true })).toBeVisible();
  await expect(page.getByText('raw.S00E01.mkv', { exact: true })).toHaveCount(0);
  await expect(page.getByText('季集不明确', { exact: true })).toBeVisible();
  await noOverflow(page);
});

test('manual recognition shows applied identity even if media read fails and preserves edits', async ({
  page,
}) => {
  await fixtures(page);
  await directories(page);
  let requests = 0;
  await page.route('**/api/cloud/plans', (route) => {
    requests++;
    const override = route.request().postDataJSON().overrides['12'];
    return fulfill(route, {
      status: 'success',
      plan: {
        id: 'p1',
        expires_in: 900,
        items: override
          ? []
          : [
              {
                source: { id: '12', name: 'raw.S02E03.mkv' },
                media: {
                  kind: 'tv',
                  title: '剧集乙',
                  tmdb_id: '42',
                  season: 2,
                  episode: 3,
                  name: '剧集乙.S02E03.mkv',
                },
              },
            ],
        skipped: override
          ? [
              {
                file_id: '12',
                name: 'raw.S02E03.mkv',
                override,
                recognition: { ...override, title: '剧集乙' },
                code: 'probe_http_403',
                stage_label: '媒体信息',
                reason: '115 媒体读取返回 HTTP 403，保留源文件',
              },
            ]
          : [],
      },
    });
  });
  await page.goto('/#/organize/workspace');
  await page.getByRole('button', { name: '扫描并预览整理' }).click();
  await page.getByRole('button', { name: '修正识别', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('combobox', { name: '类型', exact: true })).toHaveValue('tv');
  await expect(dialog.getByLabel('指定季号')).toHaveValue('2');
  await dialog.getByLabel('指定集号').fill('4');
  await dialog.getByRole('button', { name: '采用此识别并重新预览' }).click();
  await expect(page.getByText(/本页已保留 1 项人工修正/)).toBeVisible();
  await expect(page.getByText(/作品已核实：剧集乙/)).toContainText('S02E04');
  await expect(page.getByText('错误码：probe_http_403')).toBeVisible();
  await page.getByRole('button', { name: '修正识别', exact: true }).click();
  await expect(dialog.getByLabel('指定集号')).toHaveValue('4');
  expect(requests).toBe(2);
  await noOverflow(page);
});
