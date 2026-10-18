import { test, expect, type Page } from '@playwright/test';
import { fixtures, fulfill } from './fixtures';

async function eventFeed(page: Page) {
  await page.addInitScript(() => {
    const streams: { url: string; stream: EventTarget }[] = [];
    (window as unknown as { activityStreams: typeof streams }).activityStreams = streams;
    class Feed extends EventTarget {
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(public url: string) {
        super();
        streams.push({ url, stream: this });
        queueMicrotask(() => this.onopen?.());
      }
      close() {
        const i = streams.findIndex((row) => row.stream === this);
        if (i >= 0) streams.splice(i, 1);
      }
    }
    window.EventSource = Feed as unknown as typeof EventSource;
  });
}
async function activity(page: Page) {
  await page.evaluate(() => {
    const streams = (
      window as unknown as { activityStreams: { url: string; stream: EventTarget }[] }
    ).activityStreams;
    streams
      .filter((row) => row.url === '/api/activity/events')
      .forEach((row) => row.stream.dispatchEvent(new MessageEvent('activity', { data: '[1]' })));
  });
}

test('log events update immediately, pause preserves cursor, task query scopes reads', async ({
  page,
}) => {
  await fixtures(page);
  await eventFeed(page);
  let cursor = 1;
  const reads: URL[] = [];
  await page.route('**/api/logs?**', (r) => {
    const url = new URL(r.request().url());
    reads.push(url);
    const after = Number(url.searchParams.get('after'));
    return fulfill(r, {
      status: 'success',
      cursor,
      logs:
        after < cursor
          ? [
              {
                id: cursor,
                ts: '2026-10-18T18:30:00',
                level: 'INFO',
                source: 'cloud_strm',
                message: `文件 ${cursor} 已生成`,
                details: { job_id: 'abcdef' },
              },
            ]
          : [],
    });
  });
  await page.goto('/#/logs?task=abcdef');
  await expect(page.getByText('文件 1 已生成', { exact: true })).toBeVisible();
  expect(reads[0]!.searchParams.get('task')).toBe('abcdef');
  cursor = 2;
  await activity(page);
  await expect(page.getByText('文件 2 已生成', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  cursor = 3;
  const count = reads.length;
  await activity(page);
  await expect(page.getByText('文件 3 已生成', { exact: true })).toHaveCount(0);
  expect(reads.length).toBe(count);
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await expect(page.getByText('文件 3 已生成', { exact: true })).toBeVisible();
  expect(reads.at(-1)!.searchParams.get('after')).toBe('2');
});

test('task details refresh on event and share one feed with the timeline', async ({ page }) => {
  await fixtures(page);
  await eventFeed(page);
  let done = false;
  await page.route('**/api/cloud/jobs/job-a', (r) =>
    fulfill(r, {
      status: 'success',
      job: {
        id: 'job-a',
        kind: 'organize',
        state: done ? 'success' : 'queued',
        message: done ? '归档已完成' : '等待执行',
      },
    }),
  );
  await page.route('**/api/cloud/jobs/job-a/timeline', (r) =>
    fulfill(r, {
      status: 'success',
      items: done
        ? [
            {
              id: 2,
              ts: '2026-10-18T18:30:00',
              level: 'INFO',
              stage: 'strm',
              message: '本批 2 个 STRM 已生成',
              details: { elapsed_ms: 1200 },
            },
          ]
        : [],
    }),
  );
  await page.goto('/#/tasks?job=job-a');
  await expect(page.getByRole('dialog')).toContainText('等待执行');
  done = true;
  await activity(page);
  await expect(page.getByRole('dialog')).toContainText('归档已完成');
  await expect(page.getByRole('dialog')).toContainText('本批 2 个 STRM 已生成');
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { activityStreams: { url: string }[] }).activityStreams.filter(
          (r) => r.url === '/api/activity/events',
        ).length,
    ),
  ).toBe(1);
});

test('playback check shows separate verified internal and external links', async ({ page }) => {
  await fixtures(page);
  await page.route('**/api/cloud/assets?**', (r) =>
    fulfill(r, {
      status: 'success',
      items: [{ id: 'asset-a', state: 'active', path: '/strm/local/Movie.strm' }],
    }),
  );
  let checked = 0;
  await page.route('**/api/cloud/assets/asset-a/playback/check', (r) => {
    checked++;
    return fulfill(r, {
      status: 'success',
      checked: true,
      mode: '302',
      message: '源文件解析已核验',
      internal_link: 'http://nas.local/api/cloud/play/a?sig=x',
      external_link: 'https://play.example/api/cloud/play/a?sig=x',
    });
  });
  await page.goto('/#/media/strm');
  await page.getByLabel('选择已生成的资源').selectOption('asset-a');
  expect(checked).toBe(0);
  await page.getByRole('button', { name: '核验源文件与播放解析', exact: true }).click();
  await expect(page.getByText('302 解析通过', { exact: true })).toBeVisible();
  await expect(page.getByLabel('外网播放链接', { exact: true })).toHaveValue(
    'https://play.example/api/cloud/play/a?sig=x',
  );
  expect(checked).toBe(1);
});

test('delay units save seconds and leave unrelated module directories unchanged', async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto('/#/media/strm');
  await page.getByRole('button', { name: '分钟', exact: true }).click();
  await page.getByLabel('STRM 延迟生成（分钟）', { exact: true }).fill('2');
  await page.getByRole('button', { name: '保存输出与联动设置', exact: true }).click();
  await page.getByLabel('我已核对上面的操作范围和影响').check();
  await page.getByRole('button', { name: '确认执行', exact: true }).click();
  await expect
    .poll(() => state.writes.find((row) => row.path === '/api/cloud/settings')?.body)
    .toMatchObject({ strm_delay_seconds: 120 });
  expect(state.writes.some((row) => row.path.endsWith('/directories'))).toBe(false);
});

test('successful archived governance task shows a note and keeps its success state', async ({
  page,
}) => {
  await fixtures(page);
  await page.route('**/api/tasks/recent', (r) =>
    fulfill(r, {
      status: 'success',
      items: [{ id: 'old-scan', kind: 'stats', source: 'web', status: 'success', ts: 1792317600 }],
    }),
  );
  await page.route('**/api/task/old-scan', (r) =>
    fulfill(r, {
      id: 'old-scan',
      status: 'success',
      archived: true,
      cancellable: false,
      archive_note: '历史任务仅保留状态摘要',
      logs: [],
    }),
  );
  await page.goto('/#/tasks');
  await page.getByRole('button', { name: /媒体统计/ }).click();
  await expect(page.locator('.task-receipt')).toContainText('历史任务仅保留状态摘要');
  await expect(page.locator('.task-receipt .task-receipt-state')).toHaveText('已完成');
  await expect(page.locator('.task-receipt .inline-error')).toHaveCount(0);
});
