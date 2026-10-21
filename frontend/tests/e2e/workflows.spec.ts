import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';
import { routes } from './routes';

test('all functional routes render without read errors, auto writes or horizontal overflow', async ({
  page,
}) => {
  test.setTimeout(60000);
  const state = await fixtures(page),
    errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  for (const [route, title] of routes) {
    await page.goto('/#/' + route);
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    await page.waitForLoadState('networkidle');
    await expect(page.locator('.inline-error')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByText('页面出现异常，请刷新页面重试。', { exact: true })).toHaveCount(0);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      route,
    ).toBe(true);
  }
  expect(errors).toEqual([]);
  expect(state.writes.filter((w) => !w.path.includes('library-status'))).toEqual([]);
});

test('receive preview expires after editing input and switching account', async ({ page }) => {
  const state = await fixtures(page);
  await page.route('**/api/cloud/resources/preview', (r) =>
    fulfill(r, { status: 'success', plan: { id: 'sealed-plan', items: [{ name: '影片.mkv' }] } }),
  );
  await page.goto('/#/resources/receive');
  await page.getByLabel('资源内容').fill('magnet:?xt=urn:btih:test');
  await page.getByRole('button', { name: '识别资源并预览' }).click();
  await expect(page.getByRole('heading', { name: '操作预览' })).toBeVisible();
  await page.getByLabel('资源内容').fill('magnet:?xt=urn:btih:changed');
  await expect(page.getByRole('heading', { name: '操作预览' })).toHaveCount(0);
  await page.getByRole('button', { name: '识别资源并预览' }).click();
  await expect(page.getByRole('heading', { name: '操作预览' })).toBeVisible();
  await page.getByLabel('当前 115 账号').selectOption('account-b');
  await expect(page.getByRole('heading', { name: '操作预览' })).toHaveCount(0);
  expect(state.writes.some((w) => w.path.endsWith('/apply'))).toBe(false);
});

test('share STRM uses the independent preview and apply contract', async ({ page }) => {
  await fixtures(page);
  const writes: { path: string; body: any }[] = [];
  await page.route('**/api/cloud/shares/preview', (r) =>
    fulfill(r, {
      status: 'success',
      preview_id: 'share-seal',
      items: [{ id: '1', name: '电影.mkv', is_dir: false, size: 1000 }],
    }),
  );
  await page.route('**/api/cloud/shares/strm/preview', (r) => {
    writes.push({ path: 'preview', body: r.request().postDataJSON() });
    return fulfill(r, {
      status: 'success',
      plan: { id: 'share-strm-plan', items: [{ name: '电影.mkv' }] },
    });
  });
  await page.route('**/api/cloud/shares/strm/apply', (r) => {
    writes.push({ path: 'apply', body: r.request().postDataJSON() });
    return fulfill(r, { status: 'success', job_id: 'accepted-job' }, 202);
  });
  await page.route('**/api/cloud/jobs/accepted-job', (r) =>
    fulfill(r, {
      status: 'success',
      job: { id: 'accepted-job', state: 'running', kind: 'share_strm' },
    }),
  );
  await page.goto('/#/resources/share');
  await page.getByLabel('115 分享链接').fill('https://115.com/s/test');
  await page.getByRole('button', { name: '读取分享', exact: true }).click();
  await page.getByLabel('选择电影.mkv').check();
  await page.getByRole('button', { name: '识别并预览分享 STRM' }).click();
  await page.getByRole('button', { name: '核对并提交' }).click();
  await expect(page.getByRole('button', { name: '确认执行' })).toBeDisabled();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect(
    page.locator('.task-receipt .mono').filter({ hasText: 'accepted-job' }),
  ).toBeVisible();
  expect(writes).toEqual([
    { path: 'preview', body: { preview_id: 'share-seal', file_ids: ['1'], overrides: {} } },
    { path: 'apply', body: { plan_id: 'share-strm-plan', confirmed: true } },
  ]);
  await expect(page.getByText('已确认入库', { exact: true })).toHaveCount(0);
});

test('single movie delete requires preview, exact phrase and an operation id', async ({ page }) => {
  await fixtures(page);
  const writes: any[] = [];
  let finished = false;
  await page.route('**/api/emby/movie/delete_by_tmdb', (r) => {
    const body = r.request().postDataJSON();
    writes.push(body);
    return fulfill(
      r,
      body.dry_run
        ? { status: 'success', count: 3, source_preview_id: 'source-seal' }
        : { status: 'success', operation_id: body.operation_id, state: 'running' },
      body.dry_run ? 200 : 202,
    );
  });
  await page.route('**/api/emby/delete/progress/*', (r) =>
    fulfill(r, {
      status: 'success',
      state: finished ? 'done' : 'running',
      processed: finished ? 3 : 0,
      total: 3,
      ...(finished
        ? {
            result: {
              status: 'partial',
              strm_removed: 2,
              retained: 1,
              failed: 1,
              message: '部分文件已保留，请核对结果',
            },
          }
        : {}),
    }),
  );
  await page.goto('/#/media/library');
  await page.getByRole('button', { name: '电影片库', exact: true }).click();
  await page.getByRole('button', { name: '查看星际穿越详情' }).click();
  await page.getByRole('button', { name: '预览删除本地库', exact: true }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await expect(page.getByRole('button', { name: '确认执行' })).toBeDisabled();
  await page.getByLabel('输入“删除本地库及源文件”确认').fill('删除本地库及源文件');
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({
    tmdb_id: '1',
    target: 'local',
    dry_run: false,
    async_delete: true,
    source_preview_id: 'source-seal',
  });
  expect(writes[1].operation_id).toMatch(/^[\da-f]{32}$/);
  await expect(
    page.getByRole('button', { name: '隐藏进度（删除继续）', exact: true }),
  ).toBeVisible();
  finished = true;
  await page.getByRole('dialog').getByRole('button', { name: '核实状态', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.task-progress-counts')).toContainText('保留 1');
  await expect(page.getByRole('dialog').locator('.task-progress-counts')).toContainText('失败 1');
  await page.getByRole('dialog').getByRole('button', { name: '关闭结果', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.task-receipt')).toHaveCount(0);
  await expect(
    page.getByRole('dialog').getByRole('button', { name: '预览删除本地库', exact: true }),
  ).toBeEnabled();
  expect(writes).toHaveLength(2);
});

test('interrupted mutation stays locked after editing inputs and never retries automatically', async ({
  page,
}) => {
  await fixtures(page);
  let applies = 0;
  await page.route('**/api/cloud/resources/preview', (r) =>
    fulfill(r, { status: 'success', plan: { id: 'plan', items: [{ name: '电影' }] } }),
  );
  await page.route('**/api/cloud/resources/apply', (r) => {
    applies++;
    return fulfill(r, { detail: '网关中断' }, 502);
  });
  await page.goto('/#/resources/receive');
  await page.getByLabel('资源内容').fill('magnet:?xt=test');
  await page.getByRole('button', { name: '识别资源并预览' }).click();
  await page.getByRole('button', { name: '核对并提交' }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect(page.getByRole('button', { name: '确认执行' })).toBeDisabled();
  await page.getByRole('button', { name: '返回核对' }).click();
  await page.getByLabel('资源内容').fill('magnet:?xt=changed');
  await expect(page.getByRole('button', { name: '识别资源并预览' })).toBeDisabled();
  expect(applies).toBe(1);
});

test('directory configuration carries current revision and reports 409 without retry', async ({
  page,
}) => {
  await fixtures(page);
  let writes = 0;
  await page.route('**/api/cloud/accounts/account-a/directories', (r) =>
    r.request().method() === 'POST'
      ? (writes++,
        expect(r.request().postDataJSON().expected_revision).toBe(3),
        fulfill(r, { detail: '目录配置已变化' }, 409))
      : fulfill(r, {
          status: 'success',
          revision: 3,
          directories: { source: { id: '100', name: '影视' }, target: { id: '101', name: '归档' } },
        }),
  );
  await page.goto('/#/organize/workspace');
  await expect(page.getByRole('button', { name: '保存整理目录' })).toBeDisabled();
  await page
    .locator('.organize-directory-cards .directory-purpose-card')
    .first()
    .getByRole('button', { name: '选择目录', exact: true })
    .click();
  await page.getByRole('button', { name: '使用当前目录', exact: true }).click();
  await page.getByRole('button', { name: '保存整理目录' }).click();
  await expect(page.getByText(/目录配置已变化，请刷新后核对再保存/)).toBeVisible();
  expect(writes).toBe(1);
});

test('batch all-selection uses a sealed preview and never limits deletion to the loaded page', async ({
  page,
}) => {
  await fixtures(page);
  let previewBody: any, executeBody: any;
  await page.route('**/api/emby/series/batch-delete/preview/start', (r) => {
    previewBody = r.request().postDataJSON();
    return fulfill(r, { status: 'success', job_id: 'preview-batch' });
  });
  await page.route('**/api/emby/series/batch-delete/preview/status?*', (r) =>
    fulfill(r, {
      status: 'done',
      processed: 120,
      total: 120,
      result: {
        status: 'success',
        preview_id: 'sealed-all',
        count: 120,
        strm_count: 600,
        batch_count: 2,
      },
    }),
  );
  await page.route('**/api/emby/series/batch-delete/execute', (r) => {
    executeBody = r.request().postDataJSON();
    return fulfill(
      r,
      { status: 'success', operation_id: executeBody.operation_id, state: 'running' },
      202,
    );
  });
  await page.route('**/api/emby/delete/progress/*', (r) =>
    fulfill(r, { status: 'success', state: 'running' }),
  );
  await page.goto('/#/media/library');
  // 范围改为 1.2.12 式芯片：本地+分享 / 仅本地 / 仅分享
  await page.getByRole('group', { name: '范围' }).getByRole('button', { name: '仅分享' }).click();
  await page.getByRole('button', { name: '多选清理' }).click();
  await page.getByRole('button', { name: '选择全部筛选结果' }).click();
  await page.getByRole('button', { name: '预览批量删除 · 分享库' }).click();
  await expect(page.getByRole('dialog', { name: '确认片库批量删除' })).toBeVisible();
  expect(previewBody.selection).toBe('all');
  expect(previewBody).not.toHaveProperty('series_ids');
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByLabel('输入“删除分享库”确认').fill('删除分享库');
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect.poll(() => executeBody?.preview_id).toBe('sealed-all');
  expect(executeBody).toMatchObject({ target: 'share', confirm: true, confirmation: '删除分享库' });
});

test('cloud settings save only the edited field and retain full quality and naming state', async ({
  page,
}) => {
  await fixtures(page);
  const defaults = await import('./defaults.json', { with: { type: 'json' } });
  let body: any;
  await page.route('**/api/cloud/settings', (r) => {
    if (r.request().method() === 'POST') {
      body = r.request().postDataJSON();
      return fulfill(r, { status: 'success', settings: { ...defaults.default.cloud, ...body } });
    }
    return fulfill(r, { status: 'success', settings: defaults.default.cloud });
  });
  await page.goto('/#/settings/cloud');
  await page.getByLabel('个人库 NAS STRM 输出目录', { exact: true }).fill('/strm/custom');
  await page.getByRole('button', { name: '保存 1 项修改' }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行' }).click();
  await expect
    .poll(() => body)
    .toEqual({ output_root: '/strm/custom', _expected: { output_root: '/strm/local' } });
  await expect(page.getByRole('button', { name: '保存 0 项修改' })).toBeDisabled();
});

test('late account write does not place its receipt or directories into another account', async ({
  page,
}) => {
  await fixtures(page);
  let resolve: (() => void) | undefined;
  await page.route('**/api/cloud/accounts/account-a/directories', async (r) => {
    if (r.request().method() === 'POST') {
      await new Promise<void>((done) => (resolve = done));
      return fulfill(r, {
        status: 'success',
        message: '旧账号已保存',
        revision: 4,
        directories: {},
      });
    }
    return fulfill(r, {
      status: 'success',
      revision: 3,
      directories: { source: { id: '100', name: '影视' }, target: { id: '101', name: '归档' } },
    });
  });
  await page.goto('/#/organize/workspace');
  await expect(page.getByRole('button', { name: '保存整理目录' })).toBeDisabled();
  await page
    .locator('.organize-directory-cards .directory-purpose-card')
    .first()
    .getByRole('button', { name: '选择目录', exact: true })
    .click();
  await page.getByRole('button', { name: '使用当前目录', exact: true }).click();
  await page.getByRole('button', { name: '保存整理目录' }).click();
  await expect.poll(() => !!resolve).toBe(true);
  await page.getByLabel('当前 115 账号').selectOption('account-b');
  resolve!();
  await expect(page.getByText('旧账号已保存', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('当前 115 账号')).toHaveValue('account-b');
  await expect(page.getByRole('textbox', { name: '待整理目录 选择目录', exact: true })).toHaveValue(
    '目录0',
  );
});
