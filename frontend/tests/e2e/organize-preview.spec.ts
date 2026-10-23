import { test, expect, type Page } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

const jid = 'a'.repeat(32),
  pid = 'b'.repeat(32);
async function previewFixture(page: Page) {
  const base = await fixtures(page);
  let state = 'running',
    scans = 0,
    applies = 0,
    stops = 0;
  const input = {
    account_id: 'account-a',
    source_cid: '1',
    target_cid: '2',
    unknown_cid: null,
    overrides: {},
  };
  await page.route('**/api/cloud/accounts/*/directories', (route) =>
    fulfill(route, {
      status: 'success',
      revision: 1,
      directories: { source: { id: '1', name: '待整理' }, target: { id: '2', name: '归档' } },
    }),
  );
  await page.route('**/api/cloud/plans/scan', (route) => {
    scans++;
    Object.assign(input, route.request().postDataJSON());
    return fulfill(route, { status: 'success', job_id: jid }, 202);
  });
  await page.route(`**/api/cloud/jobs/${jid}`, (route) =>
    fulfill(route, {
      status: 'success',
      job: {
        id: jid,
        kind: 'organize_preview',
        state,
        preview_input: input,
        result: state === 'success' ? { plan_id: pid } : {},
        message: state === 'running' ? '正在扫描预览' : '预览完成，尚未移动文件',
        progress: { phase: '识别预览', total: 29, processed: 12 },
      },
    }),
  );
  await page.route(`**/api/cloud/plans/${pid}`, (route) =>
    fulfill(route, {
      status: 'success',
      plan: {
        id: pid,
        expires_in: 880,
        items: [
          {
            source: { id: '4', name: 'raw.S04E01.mkv' },
            media: { title: '剧集', kind: 'tv', season: 4, episode: 1, name: '剧集.S04E01.mkv' },
          },
        ],
      },
    }),
  );
  await page.route('**/api/cloud/plans/apply', (route) => {
    applies++;
    expect(route.request().postDataJSON()).toEqual({
      plan_id: pid,
      confirmed: true,
      generate: true,
    });
    return fulfill(route, { status: 'success', job_id: 'job-a' }, 202);
  });
  await page.route(`**/api/cloud/jobs/${jid}/stop`, (route) => {
    stops++;
    state = 'stopped';
    return fulfill(route, { status: 'success', stop_requested: true });
  });
  return {
    base,
    complete: () => (state = 'success'),
    counts: () => ({ scans, applies, stops }),
  };
}

test('long scan survives 120 seconds and reload, then requires explicit confirmation to organize', async ({
  page,
}) => {
  const f = await previewFixture(page);
  await page.clock.install();
  await page.goto('/#/organize/workspace');
  await page.getByRole('button', { name: '扫描并预览整理' }).click();
  await expect(page).toHaveURL(new RegExp(`preview_job=${jid}`));
  await expect(page.getByText('识别预览 · 12 / 29')).toBeVisible();
  await page.clock.fastForward(130000);
  await expect(page.getByRole('button', { name: '扫描预览进行中…' })).toBeDisabled();
  expect(f.counts()).toEqual({ scans: 1, applies: 0, stops: 0 });
  await page.reload();
  await expect(page.getByText('识别预览 · 12 / 29')).toBeVisible();
  expect(f.counts().scans).toBe(1);
  f.complete();
  await page.getByRole('button', { name: '核实状态', exact: true }).click();
  await expect(page.getByText('剧集.S04E01.mkv', { exact: true })).toBeVisible();
  const apply = page.getByRole('button', { name: '确认整理（改名并移动）' });
  await expect(apply).toBeEnabled();
  expect(f.counts().applies).toBe(0);
  await apply.click();
  const dialog = page.getByRole('dialog', { name: '执行整理归档' });
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(f.counts()).toEqual({ scans: 1, applies: 1, stops: 0 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('stopping a preview never submits an organization task', async ({ page }) => {
  const f = await previewFixture(page);
  await page.goto('/#/organize/workspace');
  await page.getByRole('button', { name: '扫描并预览整理' }).click();
  await page.getByRole('button', { name: '停止扫描预览', exact: true }).click();
  await page.getByRole('button', { name: '核实状态', exact: true }).click();
  await expect(page.getByRole('button', { name: '扫描并预览整理' })).toBeEnabled();
  await expect(page.getByRole('button', { name: '确认整理（改名并移动）' })).toHaveCount(0);
  expect(f.counts()).toEqual({ scans: 1, applies: 0, stops: 1 });
});
