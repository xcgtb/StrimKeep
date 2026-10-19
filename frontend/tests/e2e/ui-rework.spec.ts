import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('organize hub retains the source-deletion confirmation before saving', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/organize');
  await expect(page.getByRole('heading', { name: '影视整理', exact: true })).toBeVisible();
  await expect(
    page.getByRole('navigation', { name: '模块功能' }).getByRole('link', { name: '扫描与整理' }),
  ).toBeVisible();
  const toggle = page.getByLabel('允许通过 Cookie 删除源文件', { exact: true });
  await expect(toggle).toBeVisible();
  await toggle.check();
  await page.getByRole('button', { name: '核对并保存', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const confirm = dialog.getByRole('button', { name: '确认执行', exact: true });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('我已核对上面的操作范围和影响').check();
  await expect(confirm).toBeDisabled();
  expect(state.writes).toHaveLength(0);
  await dialog.getByLabel('输入“启用源文件治理”确认').fill('启用源文件治理');
  await confirm.click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toEqual({
    path: '/api/cloud/settings',
    body: { official_delete_enabled: true },
  });
});

test('STRM workspace saves its schedule, timezone and probe setting in place', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/media/strm');
  await page.getByLabel('STRM 全量同步（Cron，五段）', { exact: true }).fill('0 3 * * *');
  await page.getByLabel('全量同步时区', { exact: true }).fill('UTC');
  await page.getByLabel('拦截飞牛媒体探测', { exact: true }).check();
  await page.getByRole('button', { name: '保存输出与联动设置', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('我已核对上面的操作范围和影响').check();
  await dialog.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect
    .poll(() => state.writes.find((r) => r.path === '/api/cloud/settings')?.body)
    .toEqual({
      strm_scan_cron: '0 3 * * *',
      strm_scan_timezone: 'UTC',
      strm_block_feiniu_ua: true,
    });
  expect(state.writes.some((r) => r.path.endsWith('/directories'))).toBe(false);
});

test('Cookie-only account reports its actual source-deletion capability', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/cloud/status', (route) =>
    fulfill(route, {
      status: 'success',
      accounts: [
        {
          id: 'a1',
          name: 'Cookie 账号',
          revision: 1,
          authorization: {
            cookie: true,
            cookie_delete: true,
            open: false,
            official_delete: false,
            device: 'ipad',
          },
        },
      ],
    }),
  );
  await page.goto('/#/settings/accounts');
  await expect(page.getByText('源文件删除（Cookie）', { exact: true })).toBeVisible();
  await expect(
    page.getByText('已保存 Cookie，可按文件 ID 删源；执行前仍需开启删除并核验文件身份', {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText('未连接，官方接口与按 ID 删源不可用', { exact: true })).toHaveCount(
    0,
  );
});

for (const width of [320, 390, 430]) {
  test(`playback select and verification stay inside their card at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixtures(page);
    const longName = 'Very.Long.Movie.2026.2160p.Dolby.Vision.TrueHD.Atmos.'.repeat(8) + 'strm';
    await page.route('**/api/cloud/assets?**', (route) =>
      fulfill(route, {
        status: 'success',
        items: [{ id: 'asset-a', state: 'active', path: '/strm/local/' + longName }],
      }),
    );
    await page.route('**/api/cloud/assets/asset-a/playback/check', (route) =>
      fulfill(route, {
        status: 'success',
        checked: true,
        mode: '302',
        message: '源文件解析已核验',
        internal_link: 'http://nas.local/api/cloud/play/a?sig=' + 'a'.repeat(256),
        external_link: 'https://play.example/api/cloud/play/a?sig=' + 'b'.repeat(256),
      }),
    );
    await page.goto('/#/media/strm');
    await page.getByLabel('选择已生成的资源').selectOption('asset-a');
    await page.getByRole('button', { name: '核验源文件与播放解析', exact: true }).click();
    await expect(page.getByText('302 解析通过', { exact: true })).toBeVisible();
    const bounds = await page.locator('.playback-check').evaluate((card) => {
      const outer = card.getBoundingClientRect();
      return {
        viewportFits: document.documentElement.scrollWidth <= window.innerWidth,
        childrenFit: [...card.querySelectorAll('select,input,button')].every((el) => {
          const inner = el.getBoundingClientRect();
          return inner.left >= outer.left && inner.right <= outer.right;
        }),
      };
    });
    expect(bounds).toEqual({ viewportFits: true, childrenFit: true });
  });
}
