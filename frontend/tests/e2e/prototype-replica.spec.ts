import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('STRM output picker is read-only until module settings are confirmed and persists after reload', async ({
  page,
}) => {
  await fixtures(page);
  let settings: Record<string, unknown> = {};
  let saved: Record<string, unknown> | undefined;
  const defaults = await import('./defaults.json', { with: { type: 'json' } });
  settings = { ...defaults.default };
  await page.route('**/api/cloud/settings', async (r) => {
    if (r.request().method() === 'POST') {
      saved = r.request().postDataJSON();
      settings = { ...settings, ...saved };
    }
    return fulfill(r, { status: 'success', settings });
  });
  await page.goto('/#/media/strm');
  await page.getByText('当前配置：排期、输出、播放地址与来源', { exact: true }).click();
  await page.getByText('手动补生成与高级核验', { exact: true }).click();
  await page.getByRole('button', { name: '选择个人库 NAS STRM 输出目录', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '/strm', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'local', exact: true }).click();
  await page.getByRole('button', { name: '使用当前目录', exact: true }).click();
  await expect(page.getByLabel('个人库 NAS STRM 输出目录', { exact: true })).toHaveValue(
    '/strm/local',
  );
  expect(saved).toBeUndefined();
  await page
    .getByLabel('STRM 播放服务地址（302）', { exact: true })
    .fill('https://play.example.test');
  await page.getByLabel('STRM 延迟生成（秒）', { exact: true }).fill('30');
  await page.getByRole('button', { name: '保存输出与联动设置', exact: true }).click();
  expect(saved).toBeUndefined();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect.poll(() => saved?.public_url).toBe('https://play.example.test');
  expect(saved).toMatchObject({ output_root: '/strm/local', strm_delay_seconds: 30 });
  expect(saved).not.toHaveProperty('share_output_root');
  await page.reload();
  await page.getByText('当前配置：排期、输出、播放地址与来源', { exact: true }).click();
  await expect(page.getByLabel('STRM 延迟生成（秒）', { exact: true })).toHaveValue('30');
  await expect(page.getByLabel('STRM 播放服务地址（302）', { exact: true })).toHaveValue(
    'https://play.example.test',
  );
});

test('share output settings only update the share path and display the common generation policy', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/resources/share');
  await page.locator('.share-output-config > summary').click();
  await expect(page.getByRole('heading', { name: '分享库输出与播放', exact: true })).toBeVisible();
  await page.getByLabel('分享库 NAS STRM 输出目录', { exact: true }).fill('/strm/shared-films');
  await page.getByRole('button', { name: '保存输出与联动设置', exact: true }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect
    .poll(() => state.writes.filter((w) => w.path === '/api/cloud/settings').length)
    .toBe(1);
  expect(state.writes.find((w) => w.path === '/api/cloud/settings')!.body).toEqual({
    share_output_root: '/strm/shared-films',
    _expected: { share_output_root: '/strm/share' },
  });
});

test('accounts page has authorization and maintenance while directory controls live in the workflow', async ({
  page,
}) => {
  await fixtures(page);
  await page.goto('/#/settings/accounts');
  await expect(page.getByRole('button', { name: '获取二维码', exact: true })).toBeVisible();
  await expect(page.getByText('统一接收与待整理目录', { exact: true })).toHaveCount(0);
  await page.goto('/#/organize/workspace');
  await expect(page.getByRole('button', { name: '保存整理目录', exact: true })).toBeVisible();
  await expect(
    page.getByRole('textbox', { name: '待整理目录 选择目录', exact: true }),
  ).toBeVisible();
});

test('output directory read errors remain visible and cannot be confirmed as a successful selection', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/cloud/output-directories?**', (r) =>
    fulfill(r, { detail: '容器挂载目录暂不可访问' }, 409),
  );
  await page.goto('/#/media/strm');
  await page.getByText('当前配置：排期、输出、播放地址与来源', { exact: true }).click();
  await page.getByText('手动补生成与高级核验', { exact: true }).click();
  await page.getByRole('button', { name: '选择个人库 NAS STRM 输出目录', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('容器挂载目录暂不可访问');
  await expect(page.getByRole('button', { name: '使用当前目录', exact: true })).toBeDisabled();
});
