import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('schedule mode and cleanup scope are explicit before saving', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/media/strm');
  await expect(page.getByRole('heading', { name: '自动运行概况' })).toBeVisible();
  await expect(page.getByLabel('STRM 全量同步（Cron，五段）', { exact: true })).toBeHidden();
  await page.getByText('当前配置：排期、输出、播放地址与来源', { exact: true }).click();
  await page.getByLabel('STRM 全量同步（Cron，五段）', { exact: true }).fill('0 4 * * *');
  await page.getByLabel('启用或修改排期后的启动方式', { exact: true }).selectOption('scheduled');
  await page.getByLabel('清理连续两次扫描缺失的自建 NAS 引用', { exact: true }).uncheck();
  await page.getByRole('button', { name: '保存输出与联动设置', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('连续两次完整扫描');
  expect(state.writes).toHaveLength(0);
  await dialog.getByLabel('我已核对上面的操作范围和影响').check();
  await dialog.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect
    .poll(() => state.writes[0]?.body)
    .toEqual({
      _expected: {
        strm_scan_cron: '',
        strm_scan_start_mode: 'immediate',
        strm_reconcile_enabled: true,
      },
      strm_scan_cron: '0 4 * * *',
      strm_scan_start_mode: 'scheduled',
      strm_reconcile_enabled: false,
    });
});

test('STRM shows actual connection mappings and discrepancy instead of assuming proxy path', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/cloud/accounts/*/pipeline', (route) =>
    fulfill(route, {
      status: 'success',
      checks: [],
      emby_mappings: {
        items: [
          {
            connection_id: 'emby-1',
            name: '客厅',
            kind: 'personal',
            enabled: true,
            nas_root: '/nas/strm',
            emby_root: '/actual/library',
            sample_emby_path: '/actual/library/Movie.strm',
            consistent: false,
            message: '以入库回执为准',
          },
        ],
      },
    }),
  );
  await page.goto('/#/media/strm');
  await page.getByText('当前配置：排期、输出、播放地址与来源', { exact: true }).click();
  await expect(page.getByText('Emby：/actual/library', { exact: true })).toBeVisible();
  await expect(
    page.getByText('反代路径与此连接的入库路径不同，请核对容器挂载。入库以此连接配置为准。', {
      exact: true,
    }),
  ).toBeVisible();
});

test('share quality settings save only their own rules and enablement', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/resources/share');
  await page.getByText('分享库独立选版与洗版策略', { exact: true }).click();
  await page.getByLabel('启用分享库独立质量比较', { exact: true }).check();
  await page.getByLabel('执行方式', { exact: true }).selectOption('upgrade');
  await page.getByRole('button', { name: '保存分享库策略', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('分享库策略独立生效');
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect
    .poll(() => state.writes[0]?.body)
    .toEqual({
      share_quality_enabled: true,
      share_quality_mode: 'upgrade',
      _expected: { share_quality_enabled: false, share_quality_mode: 'preview' },
    });
});

test('receive references the saved organizer archive without an editing picker', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/cloud/shares/preview', (route) =>
    fulfill(route, {
      status: 'success',
      preview_id: 'share-seal',
      items: [{ id: '1', name: '电影.mkv', is_dir: false, size: 1000 }],
    }),
  );
  await page.goto('/#/resources/receive?tab=share');
  await page.getByLabel('115 分享链接', { exact: true }).fill('https://115.com/s/test');
  await page.getByRole('button', { name: '读取分享', exact: true }).click();
  await page.getByLabel('转存后整理', { exact: true }).check();
  await expect(page.getByText('整理归档目录：目录1', { exact: true })).toBeVisible();
  await expect(
    page.getByRole('link', { name: '到影视整理修改归档目录 →', exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('textbox', { name: '整理归档目录 选择目录', exact: true }),
  ).toHaveCount(0);
});
