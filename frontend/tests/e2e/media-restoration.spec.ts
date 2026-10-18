import { test, expect, type Page } from '@playwright/test';
import { fixtures, fulfill, movies } from './fixtures';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

async function screenshot(page: Page, name: string, project: string) {
  if (!process.env.STRIMKEEP_SCREENSHOT_DIR) return;
  await mkdir(process.env.STRIMKEEP_SCREENSHOT_DIR, { recursive: true });
  if (project === 'mobile' && name === 'explore')
    await page.getByRole('button', { name: '收起筛选', exact: true }).click();
  if (await page.getByRole('button', { name: '关闭提示', exact: true }).isVisible())
    await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  if (project === 'mobile' && name === 'explore')
    await page.locator('.poster-grid').evaluate((el) => el.scrollIntoView({ block: 'center' }));
  if (project === 'mobile' && name.startsWith('delete-'))
    await page.locator('.task-receipt').evaluate((el) => el.scrollIntoView({ block: 'center' }));
  if (project === 'mobile' && name === 'mapping')
    await page
      .locator('.mapping-selection')
      .evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  );
  await page.screenshot({
    path: path.join(process.env.STRIMKEEP_SCREENSHOT_DIR, `${name}-${project}.png`),
    fullPage: project !== 'mobile' && !name.includes('detail'),
  });
}
const action = {
  kind: 'loc',
  text: '测试剧集 S01（分享更优 → 删本地）',
  detail: '分享画质达标 18/18 集，保留分享版本。',
  files_count: 18,
  files: ['/media/local/测试剧集/S01E01.strm'],
  meta: {
    title: '测试剧集',
    season: 1,
    reason_label: '分享择优-删本地',
    match_basis: '剧名 + 年份',
    local_count: 18,
    share_count: 24,
    local_total: 18,
    share_total: 24,
    local_paths: ['/media/local/测试剧集/S01E01.strm'],
    share_paths: ['/media/share/测试剧集/S01E01.strm'],
    compare: {
      result: 1,
      episode: 1,
      decided_by: 'resolution',
      share_name: '测试剧集.2160p.mkv',
      local_name: '测试剧集.1080p.mkv',
      dims: [
        {
          key: 'resolution',
          label: '分辨率优先级',
          a: '2160p / 4K',
          b: '1080p',
          verdict: 'a',
          reason: '决胜维度',
        },
      ],
    },
  },
};
function governance(overrides: Record<string, unknown> = {}) {
  return {
    status: 'success',
    scan: {
      found: true,
      usable: true,
      ts: Date.now() / 1000,
      ttl_sec: 7200,
      remaining_sec: 7200,
      plan_id: 'plan-current',
      plan_state: 'pending',
      rule_aligned: true,
      file_guard_ready: true,
      result: {
        del_local_cnt: 1,
        del_share_cnt: 0,
        del_local_files: 18,
        del_share_files: 0,
        total_clean_cnt: 1,
        del_local_items: [action],
        protected_items: [
          { kind: 'keep', text: '多季保护作品', detail: '多季保护', files_count: 0 },
        ],
        exempted_count: 448,
        strm_counts: { local: 16078, share: 125060, total: 141138 },
      },
      ...overrides,
    },
    consistency: { library: { ts: Date.now() / 1000 }, ingest: { ts: Date.now() / 1000 } },
  };
}

test('explore shows poster facts and library labels with subscriptions only in details', async ({
  page,
}, info) => {
  await fixtures(page);
  await page.route('**/api/explore?**', (r) =>
    fulfill(r, {
      status: 'success',
      cards: [
        { ...movies[0], tmdb_id: '1', type: 'tv', title: '山海情', in_emby: false },
        {
          ...movies[1],
          tmdb_id: '2',
          type: 'tv',
          title: '人世间',
          in_emby: true,
          in_local: true,
          eps: { have: 18, total: 24, match_status: 'missing' },
        },
        {
          ...movies[2],
          tmdb_id: '3',
          type: 'tv',
          title: '漫长的季节',
          in_emby: true,
          in_share: true,
          eps: { have: 24, total: 24, match_status: 'aligned' },
        },
      ],
      total_pages: 1,
    }),
  );
  await page.goto('/#/media/explore?media=tv');
  await expect(page.locator('.poster-type')).toHaveText(['剧集', '剧集', '剧集']);
  await expect(page.locator('.poster-status')).toHaveText([
    '未入库',
    '已入库 18/24 集',
    '完整 · 24/24 集',
  ]);
  await expect(page.locator('.subscribe-button')).toHaveCount(0);
  await expect(page.locator('.poster-card').nth(0).locator('.library-tags')).toHaveCount(0);
  await expect(page.locator('.poster-card').nth(1).locator('.library-tags')).toHaveText('本地库');
  await expect(page.locator('.poster-card').nth(2).locator('.library-tags')).toHaveText('分享库');
  await expect(page.locator('.poster-caption .poster-episodes')).toHaveCount(0);
  await expect(page.locator('.media-compact .poster-meta span')).toHaveText([
    '2026',
    '剧集',
    '2025',
    '剧集',
    '2024',
    '剧集',
  ]);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '查看漫长的季节详情', exact: true }).click();
  const detail = page.getByRole('dialog');
  await detail.getByRole('button', { name: '添加追更订阅', exact: true }).click();
  await expect(detail.getByRole('button', { name: '取消追更', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await detail.getByRole('button', { name: '取消追更', exact: true }).click();
  await expect(detail.getByRole('button', { name: '添加追更订阅', exact: true })).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  await expect(page.locator('.subscribe-button')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await screenshot(page, 'explore', info.project.name);
});

test('mapping offers visible target selection and poster taps select without opening details', async ({
  page,
}, info) => {
  const state = await fixtures(page);
  await page.goto('/#/media/library');
  await page.getByRole('button', { name: '多选清理', exact: true }).click();
  await page.getByRole('button', { name: '分享库（仅 STRM）', exact: true }).click();
  await expect(page.locator('.poster-type').first()).toHaveText('剧集');
  await expect(page.locator('.poster-episodes').first()).toHaveText('已入库 18/24 集');
  await page.getByRole('button', { name: '选择星际穿越海报', exact: true }).click();
  await page.getByRole('button', { name: '选择山海之间', exact: true }).click();
  await expect(page.getByText('已选择 2 部', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '预览批量删除 · 分享库' })).toBeEnabled();
  expect(state.writes).toEqual([]);
  await screenshot(page, 'mapping', info.project.name);
});

for (const target of ['local', 'share']) {
  test(`mapping ${target} deletion shows sealed preview and real progress/result`, async ({
    page,
  }, info) => {
    await fixtures(page);
    let executeCount = 0;
    let executeBody: Record<string, unknown> | undefined;
    await page.route('**/api/emby/series/batch-delete/preview/start', (r) =>
      fulfill(r, { status: 'accepted', job_id: 'preview' }),
    );
    await page.route('**/api/emby/series/batch-delete/preview/status?*', (r) =>
      fulfill(r, {
        status: 'done',
        result: {
          status: 'success',
          preview_id: 'sealed',
          count: 1,
          strm_count: 18,
          batch_count: 1,
          batch_size: target === 'local' ? 20 : 60,
        },
      }),
    );
    await page.route('**/api/emby/series/batch-delete/execute', (r) => {
      executeCount++;
      executeBody = r.request().postDataJSON();
      return fulfill(r, { status: 'accepted', operation_id: executeBody!.operation_id }, 202);
    });
    let done = false;
    await page.route('**/api/emby/delete/progress/*', (r) =>
      fulfill(r, {
        status: 'success',
        state: done ? 'done' : 'running',
        total: 1,
        processed: done ? 1 : 0,
        strm_removed: done ? 18 : 7,
        cloud_removed: target === 'local' ? 7 : 0,
        batch_index: 1,
        batch_total: 1,
        ...(done
          ? {
              result: {
                status: 'success',
                strm_removed: 18,
                cloud_removed: target === 'local' ? 18 : 0,
                retained: 0,
                failed: 0,
                message: '删除已完成',
              },
            }
          : {}),
      }),
    );
    await page.goto('/#/media/library');
    await page.getByRole('button', { name: '多选清理', exact: true }).click();
    await page
      .getByRole('button', {
        name: target === 'local' ? '本地库（联动源文件）' : '分享库（仅 STRM）',
        exact: true,
      })
      .click();
    await page.getByRole('button', { name: '选择星际穿越海报', exact: true }).click();
    await page
      .getByRole('button', { name: `预览批量删除 · ${target === 'local' ? '本地库' : '分享库'}` })
      .click();
    const dialog = page.getByRole('dialog', { name: '确认片库批量删除' });
    await expect(dialog).toContainText('18');
    expect(executeBody).toBeUndefined();
    await page.getByLabel('我已核对上面的操作范围和影响').check();
    const phrase = target === 'local' ? '删除本地库及源文件' : '删除分享库';
    await page.getByLabel(`输入“${phrase}”确认`).fill(phrase);
    await page.getByRole('button', { name: '确认执行', exact: true }).click();
    await expect(page.getByRole('progressbar', { name: '片库删除进度' })).toHaveAttribute(
      'value',
      '0',
    );
    expect(executeBody).toMatchObject({
      preview_id: 'sealed',
      target,
      confirmation: phrase,
      confirm: true,
    });
    expect(executeBody!.operation_id).toMatch(/^[0-9a-f]{32}$/);
    await expect(page.locator('.task-progress-counts')).toContainText('STRM 已删除 7');
    await expect(page.getByRole('button', { name: '退出多选' })).toBeDisabled();
    await screenshot(page, `delete-${target}`, info.project.name);
    await expect(page.locator('.task-progress-bar')).toHaveCount(0);
    await expect(page.getByRole('button', { name: '关闭结果', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: '隐藏进度（删除继续）', exact: true }).click();
    await expect(page.locator('.task-receipt')).toHaveCount(0);
    await expect(page.getByRole('button', { name: '查看删除进度', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '退出多选', exact: true })).toBeDisabled();
    done = true;
    await expect(page.getByRole('button', { name: '查看删除结果', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '退出多选', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '查看删除结果', exact: true }).click();
    await expect(page.locator('.task-progress-counts')).toContainText('STRM 已删除 18');
    await expect(
      page.locator('.task-progress').getByText('删除已完成', { exact: true }),
    ).toBeVisible();
    if (target === 'share')
      await expect(page.locator('.task-progress-counts')).not.toContainText('115');
    await expect(page.getByRole('progressbar', { name: '片库删除进度' })).toHaveCount(1);
    await expect(page.getByRole('button', { name: '关闭结果', exact: true })).toBeVisible();
    await screenshot(page, `delete-${target}-complete`, info.project.name);
    await page.getByRole('button', { name: '关闭结果', exact: true }).click();
    await expect(page.locator('.task-receipt')).toHaveCount(0);
    await expect(page.locator('.task-receipt-collapsed')).toHaveCount(0);
    await expect(page.getByRole('button', { name: '多选清理', exact: true })).toBeEnabled();
    await expect(page.locator('.selection-button')).toHaveCount(0);
    expect(executeCount).toBe(1);
    await page.getByRole('button', { name: '多选清理', exact: true }).click();
    await page.getByRole('button', { name: '选择星际穿越海报', exact: true }).click();
    await expect(
      page.getByRole('button', {
        name: `预览批量删除 · ${target === 'local' ? '本地库' : '分享库'}`,
        exact: true,
      }),
    ).toBeEnabled();
  });
}

test('governance restores comparison, deletion/retention paths and scan-clean lifecycle', async ({
  page,
}, info) => {
  await fixtures(page);
  let scanned = false,
    scans = 0,
    finished = false,
    clean: unknown;
  await page.route('**/api/governance/summary', (r) =>
    fulfill(
      r,
      scanned
        ? scans > 1
          ? governance({
              plan_id: '',
              plan_state: 'none',
              result: { del_local_cnt: 0, del_share_cnt: 0, total_clean_cnt: 0 },
            })
          : governance(finished ? { plan_state: 'done', usable: false } : {})
        : { status: 'success', scan: { found: false, usable: false, result: {} } },
    ),
  );
  await page.route('**/api/check', (r) => {
    scans++;
    scanned = true;
    return fulfill(r, { status: 'success', task_id: 'scan-task' });
  });
  await page.route('**/api/task/scan-task', (r) =>
    fulfill(r, {
      status: 'success',
      result: { total_clean_cnt: 1 },
      logs: ['扫描完成：本地待清理 1 项'],
    }),
  );
  await page.route('**/api/clean', (r) => {
    clean = r.request().postDataJSON();
    return fulfill(r, { status: 'success', task_id: 'clean-task' });
  });
  await page.route('**/api/task/clean-task', (r) =>
    fulfill(r, {
      status: finished ? 'success' : 'running',
      cancellable: false,
      logs: ['正在按计划清理文件…'],
      ...(finished ? { result: { loc_cnt: 1, sh_cnt: 0, detail: ['已删除本地 STRM 18 个'] } } : {}),
    }),
  );
  await page.goto('/#/governance');
  await expect(page.getByRole('button', { name: '核对并执行计划' })).toBeDisabled();
  await page.getByRole('button', { name: '立即治理扫描', exact: true }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect(page.getByRole('button', { name: '核对并执行计划' })).toBeEnabled();
  await expect(page.locator('.stat-card').getByText('448', { exact: true })).toBeVisible();
  await screenshot(page, 'governance', info.project.name);
  await page.getByRole('button', { name: '依据与路径' }).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('cell', { name: '2160p / 4K', exact: true })).toBeVisible();
  await expect(dialog.getByRole('cell', { name: '1080p', exact: true })).toBeVisible();
  await dialog.locator('.governance-paths').first().locator('summary').click();
  await expect(dialog.locator('.governance-paths').first()).toContainText('将删除');
  await expect(
    dialog.locator('.governance-paths').first().locator('.path-box').first(),
  ).toBeVisible();
  await screenshot(page, 'governance-detail', info.project.name);
  await page.getByRole('button', { name: '关闭详情' }).click();
  await page.getByRole('button', { name: '核对并执行计划' }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByLabel('输入“执行治理计划”确认').fill('执行治理计划');
  await page.getByRole('button', { name: '确认执行' }).click();
  expect(clean).toEqual({ plan_id: 'plan-current', dry_run: false });
  await expect(page.getByRole('button', { name: '任务执行中…' })).toBeDisabled();
  finished = true;
  await page.getByRole('button', { name: '核实状态' }).click();
  await expect(
    page.locator('.task-result-summary').getByText('已删除本地 STRM 18 个', { exact: true }),
  ).toBeVisible();
  const localCount = page.locator('.stat-card').filter({ hasText: '本地待清理' }).locator('strong');
  await expect(localCount).toHaveText('—');
  await expect(page.locator('.governance-scan-items')).not.toHaveAttribute('open');
  await expect(page.getByRole('button', { name: '核对并执行计划' })).toBeDisabled();
  await screenshot(page, 'governance-completed', info.project.name);
  await page.getByRole('button', { name: '立即治理扫描', exact: true }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect(localCount).toHaveText('0');
  await expect(page.getByRole('heading', { name: '当前诊断明细', exact: true })).toBeVisible();
  await expect(page.locator('.governance-scan-items')).toHaveAttribute('open', '');
});

for (const [name, override, message] of [
  ['changed rules', { rule_aligned: false }, '治理规则已变化'],
  ['expired plan', { ts: 1, remaining_sec: 0 }, '计划已超过有效期'],
  [
    'failed scan',
    { scan_attempt: { status: 'failed', message: '本地目录不可读' } },
    '最近扫描失败：本地目录不可读',
  ],
  ['missing guard', { file_guard_ready: false }, '计划缺少文件核验快照'],
] as const) {
  test(`governance blocks ${name} with a visible reason`, async ({ page }) => {
    await fixtures(page);
    await page.route('**/api/governance/summary', (r) => fulfill(r, governance(override)));
    await page.goto('/#/governance');
    await expect(page.locator('.governance-controls')).toContainText(message);
    await expect(page.getByRole('button', { name: '核对并执行计划' })).toBeDisabled();
  });
}

test('mobile horizontal swipes select adjacent posters while vertical movement remains scrolling', async ({
  page,
  isMobile,
}) => {
  test.skip(!isMobile, 'Touch selection');
  const state = await fixtures(page);
  await page.goto('/#/media/library');
  await page.getByRole('button', { name: '多选清理', exact: true }).click();
  await page.getByRole('button', { name: '分享库（仅 STRM）', exact: true }).click();
  await page.locator('.poster-open').first().scrollIntoViewIfNeeded();
  const result = await page.evaluate(() => {
    const grid = document.querySelector('.mapping-posters')!;
    const posters = [...grid.querySelectorAll('.poster-open')].slice(0, 3);
    const centers = posters.map((p) => {
      const r = p.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    });
    const fire = (name: string, x: number, y: number) => {
      const touch = new Touch({ identifier: 1, target: posters[0]!, clientX: x, clientY: y });
      const event = new TouchEvent(name, {
        bubbles: true,
        cancelable: true,
        touches: name === 'touchend' ? [] : [touch],
      });
      posters[0]!.dispatchEvent(event);
      return event.defaultPrevented;
    };
    fire('touchstart', centers[0]!.x, centers[0]!.y);
    const verticalPrevented = fire('touchmove', centers[0]!.x + 2, centers[0]!.y + 30);
    fire('touchend', centers[0]!.x, centers[0]!.y + 30);
    fire('touchstart', centers[0]!.x, centers[0]!.y);
    const horizontalPrevented = fire('touchmove', centers[2]!.x, centers[2]!.y);
    fire('touchend', centers[2]!.x, centers[2]!.y);
    return { verticalPrevented, horizontalPrevented };
  });
  expect(result).toEqual({ verticalPrevented: false, horizontalPrevented: true });
  await expect(page.getByText('已选择 3 部', { exact: true })).toBeAttached();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(state.writes).toEqual([]);
});

test('directory cleanup scope shortcuts submit only the chosen library root', async ({ page }) => {
  await fixtures(page);
  let body: unknown;
  await page.route('**/api/wash/empty-dirs/browse?**', (r) =>
    fulfill(r, {
      status: 'success',
      path: new URL(r.request().url()).searchParams.get('path') || '',
      roots: [
        { path: '/media/local', name: '本地库', available: true },
        { path: '/media/share', name: '分享库', available: true },
      ],
      items: [],
    }),
  );
  await page.route('**/api/wash/empty-dirs/scan', (r) => {
    body = r.request().postDataJSON();
    return fulfill(r, { status: 'success', preview: [], scanned: 2 });
  });
  await page.goto('/#/governance/cleanup');
  await page.locator('.cleanup-scope').getByRole('button', { name: '分享库', exact: true }).click();
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  expect(body).toEqual({ path: '/media/share', scope: 'directory', limit: 100 });
});

test('historical plan distinguishes its original delete list from actual execution results', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.route('**/api/plans?*', (r) =>
    fulfill(r, {
      status: 'success',
      plans: [{ id: 'past', ts: Date.now() / 1000, state: 'done', stats: { loc: 1, shr: 0 } }],
    }),
  );
  await page.route('**/api/plan/past', (r) =>
    fulfill(r, {
      status: 'success',
      plan: {
        id: 'past',
        ts: Date.now() / 1000,
        state: 'done',
        actions: [action],
        executed_result: { loc: 0, shr: 0, skipped: 1, warnings: ['文件已变化，未删除'] },
      },
    }),
  );
  await page.goto('/#/governance');
  await page.getByRole('button', { name: '查看计划详情', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('实际执行汇总', { exact: true })).toBeVisible();
  await expect(dialog.locator('.batch-summary')).toContainText('跳过 1 项');
  await dialog.locator('.plan-item-list > details > summary').click();
  await expect(dialog.locator('.governance-paths').last()).toContainText('计划删除');
  await expect(dialog.locator('.governance-detail')).not.toContainText('已删除');
  expect(state.writes).toEqual([]);
});

for (const planState of ['done', 'failed']) {
  test(`archived ${planState} scan is historical on reload and never claims pending or zero`, async ({
    page,
  }, info) => {
    await page.addInitScript(() => localStorage.setItem('strimkeep-vue-theme', 'dark'));
    const state = await fixtures(page);
    await page.route('**/api/governance/summary', (r) =>
      fulfill(r, governance({ plan_state: planState, usable: false })),
    );
    await page.goto('/#/governance');
    await expect(
      page.locator('.stat-card').filter({ hasText: '本地待清理' }).locator('strong'),
    ).toHaveText('—');
    await expect(
      page.locator('.stat-card').filter({ hasText: '分享待清理' }).locator('strong'),
    ).toHaveText('—');
    await expect(page.locator('.truth-row').filter({ hasText: '待处理' })).toContainText(
      '待重新扫描核实',
    );
    await expect(page.getByRole('heading', { name: '当前诊断明细', exact: true })).toHaveCount(0);
    await expect(page.locator('.governance-scan-items')).not.toHaveAttribute('open');
    await expect(page.getByRole('button', { name: '依据与路径', exact: true })).not.toBeVisible();
    if (planState === 'done' && process.env.STRIMKEEP_SCREENSHOT_DIR) {
      await page.locator('.stats-grid').scrollIntoViewIfNeeded();
      await page.screenshot({
        path: path.join(
          process.env.STRIMKEEP_SCREENSHOT_DIR,
          `governance-archived-${info.project.name}.png`,
        ),
        fullPage: !info.project.use.isMobile,
      });
    }
    await page.getByText('查看执行前扫描明细（历史快照）', { exact: true }).click();
    await page.getByRole('button', { name: '依据与路径', exact: true }).first().click();
    await page.getByRole('dialog').locator('.governance-paths').first().locator('summary').click();
    await expect(page.getByRole('dialog').locator('.governance-paths').first()).toContainText(
      '计划删除',
    );
    await expect(page.getByRole('button', { name: '核对并执行计划' })).toBeDisabled();
    expect(state.writes).toEqual([]);
  });
}

test('replacement ratio fills the track at 100 percent and saves the original fraction', async ({
  page,
}, info) => {
  await page.addInitScript(() => localStorage.setItem('strimkeep-vue-theme', 'dark'));
  await fixtures(page);
  let saved: Record<string, unknown> | undefined;
  await page.route('**/api/strategy', async (r) => {
    if (r.request().method() === 'GET') return r.fallback();
    saved = r.request().postDataJSON();
    return fulfill(r, { status: 'success', strategy: saved });
  });
  await page.goto('/#/governance/rules');
  const slider = page.getByRole('slider', { name: '分享达标比例', exact: true });
  await expect(slider).toHaveValue('0.9');
  await slider.focus();
  await slider.press('Home');
  await expect(page.locator('.ratio-row output')).toHaveText('50%');
  await expect(slider).toHaveCSS('--ratio-fill', '0%');
  await slider.press('ArrowRight');
  await expect(page.locator('.ratio-row output')).toHaveText('51%');
  await slider.press('End');
  await expect(slider).toHaveValue('1');
  await expect(page.locator('.ratio-row output')).toHaveText('100%');
  await expect(slider).toHaveCSS('--ratio-fill', '100%');
  await expect(slider).toHaveCSS('padding-right', '0px');
  await expect(slider).toHaveCSS('padding-left', '0px');
  await expect(slider).toHaveCSS('border-right-width', '0px');
  await slider.scrollIntoViewIfNeeded();
  await slider.evaluate((el) => el.blur());
  if (process.env.STRIMKEEP_SCREENSHOT_DIR) {
    await page.screenshot({
      path: path.join(
        process.env.STRIMKEEP_SCREENSHOT_DIR,
        `governance-ratio-full-${info.project.name}.png`,
      ),
      fullPage: !info.project.use.isMobile,
    });
  }
  await page.getByRole('button', { name: '保存治理策略', exact: true }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect.poll(() => saved?.season_replace_ratio).toBe(1);
});
