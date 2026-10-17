import { test, expect, type Page } from '@playwright/test';
import { fixtures, fulfill, library } from './fixtures';

async function fakeFeed(page: Page, fail = false) {
  await page.addInitScript((fail) => {
    const w = window as typeof window & { activeFeeds: number };
    w.activeFeeds = 0;
    class Feed extends EventTarget {
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      closed = false;
      hint = () => this.dispatchEvent(new MessageEvent('library', { data: 'changed' }));
      constructor() {
        super();
        w.activeFeeds++;
        window.addEventListener('test-library-change', this.hint);
        queueMicrotask(() => (fail ? this.onerror?.() : this.onopen?.()));
      }
      close() {
        if (this.closed) return;
        this.closed = true;
        w.activeFeeds--;
        window.removeEventListener('test-library-change', this.hint);
      }
    }
    Object.defineProperty(window, 'EventSource', { value: Feed });
  }, fail);
}

test('library events refresh every loaded page and release the stream on navigation', async ({
  page,
}) => {
  const state = await fixtures(page);
  await fakeFeed(page);
  let version = 0,
    reads = 0;
  const queries: URLSearchParams[] = [];
  const rows = Array.from({ length: 80 }, (_, i) => ({
    ...library[0],
    id: `item-${i}`,
    name: `作品${i}`,
  }));
  await page.route('**/api/library/changes?**', (r) => {
    reads++;
    return fulfill(r, {
      status: 'success',
      version,
      full: false,
      series: version ? [{ id: 'item-79' }] : [],
      movies: [],
    });
  });
  await page.route('**/api/emby/library/page?**', (r) => {
    const q = new URL(r.request().url()).searchParams;
    queries.push(q);
    const start = Number(q.get('offset')),
      limit = Number(q.get('limit'));
    const result = rows
      .slice(start, start + limit)
      .map((row) => (version && row.id === 'item-79' ? { ...row, name: '增量更新后的作品' } : row));
    return fulfill(r, {
      status: 'success',
      rows: result,
      next_offset: start + result.length,
      total: 80,
      has_more: start + result.length < 80,
    });
  });
  await page.goto('/#/media/library');
  await expect(page.locator('.poster-card')).toHaveCount(40);
  await expect(page.getByText('实时变化已连接', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '加载更多作品', exact: true }).click();
  await expect(page.locator('.poster-card')).toHaveCount(80);
  await expect.poll(() => reads).toBeGreaterThan(0);
  version = 1;
  await page.evaluate(() => window.dispatchEvent(new Event('test-library-change')));
  await expect(page.getByRole('button', { name: '增量更新后的作品', exact: true })).toBeVisible();
  await expect(page.locator('.poster-card')).toHaveCount(80);
  expect(queries.at(-1)?.get('limit')).toBe('80');
  expect(state.calls).not.toContain('GET /api/emby/library');
  await page.goto('/#/settings');
  await expect(page.getByRole('heading', { name: '连接与设置', exact: true })).toBeVisible();
  expect(
    await page.evaluate(() => (window as typeof window & { activeFeeds: number }).activeFeeds),
  ).toBe(0);
});

test('failed event streams fall back to small change reads', async ({ page }) => {
  const state = await fixtures(page);
  await fakeFeed(page, true);
  await page.clock.install();
  let version = 0,
    reads = 0;
  await page.route('**/api/library/changes?**', (r) => {
    reads++;
    return fulfill(r, { status: 'success', version, full: !!version, series: [], movies: [] });
  });
  await page.goto('/#/media/library');
  await expect(page.locator('.poster-card')).toHaveCount(8);
  await expect.poll(() => reads).toBe(1);
  const initial = state.calls.filter((r) => r === 'GET /api/emby/library/page').length;
  version = 1;
  await page.clock.runFor(15100);
  await expect
    .poll(() => state.calls.filter((r) => r === 'GET /api/emby/library/page').length)
    .toBe(initial + 1);
});

test('explore offers the complete backend genre dictionary and passes its selected name', async ({
  page,
}) => {
  await fixtures(page);
  await fakeFeed(page);
  let genre = '';
  await page.route('**/api/explore?**', (r) => {
    genre = new URL(r.request().url()).searchParams.get('genre') || '';
    return fulfill(r, { status: 'success', cards: [], total_pages: 1 });
  });
  await page.goto('/#/media/explore');
  // 1.2.12 式常驻芯片条：题材组直接可见，无需展开折叠面板
  const genres = page.getByRole('group', { name: '题材' });
  await expect(genres.getByRole('button')).toHaveCount(25);
  await genres.getByRole('button', { name: '西部', exact: true }).click();
  await expect.poll(() => genre).toBe('西部');
});

test('channel Chinese filters save exact backend country and paired genre IDs', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '添加频道', exact: true }).click();
  await page.getByLabel('频道名称', { exact: true }).fill('我的频道');
  await page.getByLabel('频道地址 / @名称 / -100 ID', { exact: true }).fill('@test_channel');
  await page.getByRole('button', { name: '中国大陆', exact: true }).click();
  await page.getByRole('button', { name: '动作', exact: true }).click();
  await page
    .locator('.directory-field')
    .getByRole('button', { name: '选择目录', exact: true })
    .click();
  await page.getByRole('button', { name: '使用当前目录', exact: true }).click();
  await page.getByRole('button', { name: '保存频道规则', exact: true }).click();
  await expect(page.getByRole('heading', { name: '编辑频道规则', exact: true })).toHaveCount(0);
  await expect.poll(() => state.writes.filter((r) => r.path.endsWith('/channels')).length).toBe(1);
  expect(state.writes.find((r) => r.path.endsWith('/channels'))?.body).toMatchObject({
    countries: ['CN'],
    genres: ['28', '10759'],
    target_cid: '0',
  });
});

test('task progress, steps beyond 100 and upgrade receipts remain accessible', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/cloud/jobs/job-a', (r) =>
    fulfill(r, {
      status: 'success',
      job: {
        id: 'job-a',
        kind: 'organize',
        state: 'failed',
        message: '需要核实',
        progress: { processed: 9, total: 105, phase: '归档', current: '正在处理文件.mkv' },
        steps: Array.from({ length: 105 }, (_, i) => ({
          step: `rename:${i}`,
          state: 'verified',
          api_key: 'never-show-secret',
        })),
        upgrades: [{ id: 'upgrade-1', state: 'needs_review', reason: '等待只读核实' }],
      },
    }),
  );
  await page.goto('/#/tasks?job=job-a');
  await expect(page.getByRole('progressbar', { name: '任务处理进度' })).toHaveAttribute(
    'value',
    '9',
  );
  await expect(page.getByText('归档 · 正在处理文件.mkv', { exact: true })).toBeVisible();
  const steps = page
    .locator('details')
    .filter({ has: page.locator('summary').filter({ hasText: '执行步骤与逐步回执' }) });
  await steps.locator('summary').click();
  await expect(steps.getByText('rename:104', { exact: true })).toHaveCount(0);
  await steps.getByRole('button', { name: '继续显示详情项目', exact: true }).click();
  await expect(steps.getByText('rename:104', { exact: true })).toBeVisible();
  await expect(page.getByText('never-show-secret', { exact: true })).toHaveCount(0);
  await page.getByText('洗版记录与保留原因', { exact: true }).click();
  await expect(page.getByText('等待只读核实', { exact: true })).toBeVisible();
  await expect(
    page.getByRole('button', { name: '核实并继续待处理洗版', exact: true }),
  ).toBeVisible();
});

test('governance lists expose candidates beyond the old 200-item cutoff', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/governance/summary', (r) =>
    fulfill(r, {
      status: 'success',
      scan: {
        usable: false,
        result: {
          del_local_items: Array.from({ length: 205 }, (_, i) => ({
            title: `治理作品${i}`,
            reason: '版本比较',
          })),
        },
      },
    }),
  );
  await page.goto('/#/governance');
  await expect(page.getByText('治理作品204', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '加载更多治理项目', exact: false }).click();
  await expect(page.getByText('治理作品204', { exact: true })).toBeVisible();
});

test('ingest cache warning and callback status are visible without resetting edited switches', async ({
  page,
}) => {
  await fixtures(page);
  await page.clock.install();
  await page.route('**/api/ingest/status', (r) =>
    fulfill(r, {
      status: 'success',
      has_cache: true,
      ts: 1792120000,
      age_sec: 45,
      warning: 'Emby 暂时不可用，保留上次结果',
      stats: { movies: 3 },
    }),
  );
  await page.goto('/#/settings');
  await expect(page.getByText('Emby 暂时不可用，保留上次结果', { exact: true })).toBeVisible();
  await page.getByRole('checkbox', { name: '启用入库监控', exact: true }).uncheck();
  await page.clock.runFor(15100);
  await expect(page.getByRole('checkbox', { name: '启用入库监控', exact: true })).not.toBeChecked();
});

test('classification and watch reports load only on request and render real response fields', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.route('**/api/library_stats', (r) =>
    fulfill(r, {
      status: 'success',
      ts: 1792120000,
      local_total: 3,
      share_total: 5,
      local_other: 0,
      share_other: 0,
      rows: [{ name: '电影', local: 3, share: 5, total: 8 }],
    }),
  );
  await page.route('**/api/played', (r) =>
    fulfill(r, {
      status: 'success',
      records: [{ name: '观看测试作品', pct: 62, text: '用户观看到 S01E03' }],
      alerts: [{ text: '今日已入库 E04' }],
    }),
  );
  await page.goto('/#/tasks/reports');
  await expect(page.getByRole('heading', { name: '入库汇报与晨报', exact: true })).toBeVisible();
  expect(state.calls).not.toContain('GET /api/library_stats');
  expect(state.calls).not.toContain('GET /api/played');
  await page.getByRole('button', { name: 'STRM 分类统计', exact: true }).click();
  await expect(page.getByRole('row').filter({ hasText: '电影' })).toContainText('8');
  await page.getByRole('button', { name: '观看进度', exact: true }).click();
  await expect(page.getByText('观看测试作品', { exact: true })).toBeVisible();
  await expect(page.getByText('今日已入库 E04', { exact: true })).toBeVisible();
});

test('NAS title search sends a keyword and renders path text without interpreting HTML', async ({
  page,
}) => {
  await fixtures(page);
  await fakeFeed(page);
  let query = '';
  await page.route('**/api/search?**', (r) => {
    query = new URL(r.request().url()).searchParams.get('q') || '';
    return fulfill(r, {
      status: 'success',
      text: '电影 /strm/local/测试 <img src=x onerror=alert(1)>',
    });
  });
  await page.goto('/#/media/library');
  await page.getByText('在 NAS 的 STRM 目录中检索作品', { exact: true }).click();
  await page.getByLabel('NAS 目录关键词', { exact: true }).fill('测试 作品');
  await page.getByRole('button', { name: '查询 NAS 目录', exact: true }).click();
  await expect.poll(() => query).toBe('测试 作品');
  await expect(page.locator('.nas-library-search pre')).toContainText('/strm/local/测试');
  await expect(page.locator('.nas-library-search img')).toHaveCount(0);
});

test('resource filters retain backend audio, subtitle, group and zero-point information', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/upstream/search', (r) =>
    fulfill(r, {
      status: 'success',
      items: [
        {
          title: '资源 A',
          provider: '115',
          resolution: '2160p',
          video_effect: 'HDR',
          unlocked: true,
          points: 0,
          audio: 'TrueHD',
          subtitles: '简中字幕',
          release_group: '测试发布组',
          transferable: true,
          handle: 'a',
        },
        {
          title: '资源 B',
          provider: '其他来源',
          resolution: '1080p',
          video_effect: 'SDR',
          unlocked: false,
        },
      ],
    }),
  );
  await page.goto('/#/settings/external');
  await page.getByLabel('作品名称', { exact: true }).fill('测试作品');
  await page.getByRole('button', { name: '搜索候选资源', exact: true }).click();
  await expect(page.getByRole('heading', { name: '资源 B', exact: true })).toBeVisible();
  await page.getByLabel('分辨率', { exact: true }).selectOption('2160p');
  await expect(page.getByRole('heading', { name: '资源 B', exact: true })).toHaveCount(0);
  const candidate = page.locator('.candidate-card').filter({ hasText: '资源 A' });
  await expect(candidate).toContainText('TrueHD');
  await expect(candidate).toContainText('简中字幕');
  await expect(candidate).toContainText('测试发布组');
  await expect(candidate).toContainText('积分 0');
});
