import { test, expect } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

test('share naming edits stay on the share page and preserve the supplied link', async ({
  page,
}) => {
  await fixtures(page);
  await page.goto('/#/resources/share');
  const input = page.getByLabel('115 分享链接', { exact: true });
  await input.fill('https://115.com/s/abcdef1');
  await page.getByRole('button', { name: '命名模板', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '命名模板', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '返回当前分享' }).click();
  await expect(dialog).not.toBeVisible();
  await expect(input).toHaveValue('https://115.com/s/abcdef1');
  await expect(page).toHaveURL(/#\/resources\/share$/);
});

test('channel tab survives a reload and files retain the selected account', async ({ page }) => {
  await fixtures(page);
  await page.goto('/#/resources/channels');
  await page.getByRole('button', { name: '万能转发', exact: true }).click();
  await expect(page).toHaveURL(/tab=forward/);
  await page.reload();
  await expect(page.getByRole('button', { name: '万能转发', exact: true })).toHaveClass(/active/);
  await page.getByLabel('当前 115 账号').selectOption('account-b');
  await page.evaluate(() => {
    location.hash = '/resources/files';
  });
  await expect(page.getByLabel('选择网盘账号')).toHaveValue('account-b');
});

test('task filters request older matching jobs from the server', async ({ page }) => {
  await fixtures(page);
  let group = '';
  await page.route('**/api/cloud/jobs?*', (route) => {
    group = new URL(route.request().url()).searchParams.get('group') || '';
    return fulfill(route, {
      status: 'success',
      items:
        group === 'review'
          ? [
              {
                id: 'older-blocked',
                kind: 'workflow',
                state: 'needs_review',
                message: '旧失败任务',
                created: 1,
              },
            ]
          : [],
      has_more: false,
    });
  });
  await page.goto('/#/tasks');
  await page.getByRole('button', { name: '需要处理', exact: true }).click();
  await expect.poll(() => group).toBe('review');
  await expect(page.getByText('旧失败任务', { exact: true })).toBeVisible();
});
