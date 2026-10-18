import { test, expect } from '@playwright/test';
import { fixtures } from './fixtures';

test('organize hub retains the source-deletion confirmation before saving', async ({ page }) => {
  const state = await fixtures(page);
  await page.goto('/#/organize');
  await expect(page.getByRole('heading', { name: '影视整理', exact: true })).toBeVisible();
  await expect(
    page.getByRole('navigation', { name: '模块功能' }).getByRole('link', { name: '扫描与整理' }),
  ).toBeVisible();
  const toggle = page.getByLabel('允许通过官方授权删除源文件', { exact: true });
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
