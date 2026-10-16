import defaults from './defaults.json' with { type: 'json' };
import type { Page, Route } from '@playwright/test';
export const movies = Array.from({ length: 12 }, (_, i) => ({
  type: i % 3 === 0 ? 'tv' : 'movie',
  title:
    ['星际穿越', '山海之间', '流浪地球', '漫长的季节', '深海', '人生大事'][i % 6] +
    (i > 5 ? ' II' : ''),
  tmdb_id: String(i + 1),
  year: 2026 - (i % 5),
  rating: 8.2,
  poster: `/api/tmdb/poster/${i}.jpg`,
  in_emby: i % 3 === 0,
  library_status: 'available',
}));
export const library = movies.slice(0, 8).map((c, i) => ({
  id: `emby${i}`,
  name: c.title,
  year: c.year,
  tmdb_id: c.tmdb_id,
  has_image: true,
  in_local: true,
  in_share: i % 2 === 0,
  have_eps: 18,
  local_eps: 12,
  share_eps: 18,
  tmdb_info: {
    poster: `/${i}.jpg`,
    match_status: i % 2 === 0 ? 'missing' : 'aligned',
    diff: i % 2 === 0 ? -6 : 0,
    declared_total: 24,
    seasons: [{ season: 1, tmdb: 24 }],
  },
  seasons: [{ season: 1, local_eps: 12, share_eps: 18 }],
}));
export async function fulfill(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
}
export async function fixtures(page: Page, initialAuth = true) {
  const state = {
    authenticated: initialAuth,
    subscriptions: [
      {
        id: '1',
        tmdb_id: '1',
        name: '星际穿越',
        enabled: true,
        have_eps: 18,
        tmdb_declared: 24,
        poster: '/api/tmdb/poster/0.jpg',
      },
    ],
    writes: [] as { path: string; body: unknown }[],
    calls: [] as string[],
  };
  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      const url = new URL(route.request().url()),
        path = url.pathname,
        method = route.request().method();
      state.calls.push(`${method} ${path}`);
      if (path === '/api/login') {
        const body = route.request().postDataJSON();
        if (body.password === 'wrong') return fulfill(route, { detail: '用户名或密码错误' }, 401);
        state.authenticated = true;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { 'set-cookie': 'strimkeep_session=test; HttpOnly; SameSite=Lax; Path=/' },
          body: '{"status":"success"}',
        });
        return;
      }
      if (!state.authenticated) return fulfill(route, { detail: '请重新登录' }, 401);
      if (method !== 'GET')
        state.writes.push({
          path,
          body: route.request().postData() ? route.request().postDataJSON() : null,
        });
      if (path.includes('/poster/')) {
        const i = Number(path.match(/(\d+)\.jpg/)?.[1] || 0);
        const colors = ['#234f72', '#38655c', '#494e7c', '#3b6678', '#5b5077', '#7a6853'];
        return route.fulfill({
          contentType: 'image/svg+xml',
          body: `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="${colors[i % colors.length]}"/><stop offset="1" stop-color="#102335"/></linearGradient></defs><rect width="400" height="600" fill="url(#g)"/><circle cx="200" cy="220" r="110" fill="none" stroke="#ffffff18" stroke-width="30"/><path d="M0 520L170 300L280 420L400 200V600H0" fill="#ffffff0b"/><text x="30" y="490" fill="#ffffffb0" font-family="sans-serif" font-size="21">STRIMKEEP CINEMA</text><text x="30" y="540" fill="#ffffff60" font-family="sans-serif" font-size="15">MEDIA COLLECTION · 2026</text></svg>`,
        });
      }
      if (path === '/api/runtime/status')
        return fulfill(route, {
          status: 'success',
          version: '1.4.7',
          readiness: { ready: true },
          memory: { rss_mib: 162 },
        });
      if (path === '/api/logout') {
        state.authenticated = false;
        return fulfill(route, { status: 'success' });
      }
      if (path === '/api/overview')
        return fulfill(route, {
          status: 'success',
          ts: 1792120000,
          stale: false,
          dashboard: {
            localCount: '18,138',
            shareCount: '140,845',
            subscriptions: { total: 12 },
            libraryHealth: { stats: { total_series: 3128 } },
            services: {
              emby: { ok: true, host: 'http://192.168.1.10:8096' },
              tmdb: { ok: true },
              telegram: { ok: true },
            },
            lastScan: { time: '10-16 17:10', total: 24, protected: 12 },
          },
          activity: { recent: movies.slice(0, 3).map((c) => ({ ...c, media_type: c.type })) },
        });
      if (path === '/api/overview/recommendations')
        return fulfill(route, { status: 'success', cards: movies.slice(0, 6) });
      if (path === '/api/overview/refresh')
        return fulfill(route, { status: 'success', refresh: { running: true } });
      if (path === '/api/explore')
        return fulfill(route, {
          status: 'success',
          cards:
            Number(url.searchParams.get('page') || 1) === 1
              ? movies
              : [{ ...movies[0], tmdb_id: '99', title: '第二页作品' }],
          total_pages: 2,
        });
      if (path === '/api/explore/library-status')
        return fulfill(route, { status: 'success', cards: [] });
      if (path === '/api/emby/library/page')
        return fulfill(route, {
          status: 'success',
          rows: library,
          total: library.length,
          next_offset: library.length,
          has_more: false,
          facts_ts: 1792120000,
          stale: false,
        });
      if (path === '/api/emby/library/item')
        return fulfill(route, {
          status: 'success',
          item: library.find((c) => c.id === url.searchParams.get('id')),
        });
      if (path === '/api/subscriptions') {
        if (method === 'POST') state.subscriptions = route.request().postDataJSON().subscriptions;
        return fulfill(route, {
          status: 'success',
          subscriptions: state.subscriptions,
          enabled: true,
          check_tmdb: true,
          calibration_interval_hours: 6,
          ingest_enabled: true,
        });
      }
      if (path === '/api/subscriptions/settings') return fulfill(route, { status: 'success' });
      if (path === '/api/cloud/status')
        return fulfill(route, {
          status: 'success',
          accounts: [
            { id: 'account-a', name: '主账号' },
            { id: 'account-b', name: '备用账号' },
          ],
        });
      if (path === '/api/cloud/files')
        return fulfill(route, {
          status: 'success',
          items:
            url.searchParams.get('cid') === '100'
              ? [{ id: '200', name: '影片.mkv', size: 1234567890, is_dir: false }]
              : [
                  { id: '100', name: '影视归档', is_dir: true },
                  { id: '101', name: '测试电影.mkv', is_dir: false, size: 3456789012 },
                ],
          total: 2,
          offset: 0,
          has_more: false,
        });
      if (path === '/api/cloud/jobs')
        return fulfill(route, {
          status: 'success',
          items: [
            {
              id: 'job-a',
              kind: 'organize',
              state: 'needs_review',
              message: '部分文件需要核对',
              updated: 1792120000,
            },
          ],
        });
      if (path === '/api/cloud/jobs/job-a')
        return fulfill(route, {
          status: 'success',
          job: {
            id: 'job-a',
            kind: 'organize',
            state: 'needs_review',
            message: '部分文件需要核对',
            steps: [{ step: 'rename:1', state: 'failed' }],
          },
        });
      if (path.endsWith('/diagnostics'))
        return fulfill(route, {
          status: 'success',
          diagnostics: {
            items: [
              {
                name: '影片.mkv',
                label: '识别或执行跳过',
                category: 'skipped',
                stage: '影视识别',
                reason: '<img src=x onerror=alert(1)> 无法识别季集',
                suggestion: '核对片名、年份与季集后重新预览。',
                rule: '季集识别规则',
              },
            ],
            total: 1,
            has_more: false,
            offset: 0,
          },
        });
      if (path === '/api/appearance') return fulfill(route, { status: 'success', appearance: {} });
      if (path === '/api/tmdb/progress')
        return fulfill(route, { status: 'success', progress: { running: false } });
      if (path === '/api/subscriptions/status')
        return fulfill(route, { status: 'success', running: false });
      if (path === '/api/subscriptions/check')
        return fulfill(route, { status: 'success', started: true });
      if (path === '/api/cloud/settings')
        return fulfill(route, { status: 'success', settings: defaults.cloud });
      if (path.endsWith('/directories') && path.startsWith('/api/cloud/accounts/'))
        return fulfill(route, {
          status: 'success',
          revision: 3,
          directories: Object.fromEntries(
            [
              'source',
              'target',
              'transfer',
              'unknown',
              'strm',
              'magnet',
              'ed2k',
              'instant',
              'bot',
              'channel',
            ].map((k, i) => [k, { id: String(100 + i), name: '目录' + i }]),
          ),
        });
      if (path.endsWith('/maintenance'))
        return fulfill(route, {
          status: 'success',
          revision: 2,
          config: {
            signin_enabled: false,
            signin_cron: '0 3 * * *',
            recycle_enabled: false,
            recycle_cron: '0 4 * * *',
            timezone: 'Asia/Shanghai',
            retention_days: 30,
          },
        });
      if (path.endsWith('/receiving'))
        return fulfill(route, {
          status: 'success',
          revision: 4,
          config: { tg_enabled: false, organize_after_transfer: false },
        });
      if (path.endsWith('/organizer-sources'))
        return fulfill(route, {
          status: 'success',
          revision: 3,
          target: { id: '101', name: '归档' },
          items: [{ id: '100', name: '接收目录', roles: ['分享转存'] }],
        });
      if (path.endsWith('/telegram-reader'))
        return fulfill(route, { status: 'success', revision: 2, connected: false, pending: false });
      if (path.endsWith('/pipeline'))
        return fulfill(route, {
          status: 'success',
          checks: [{ key: 'output', label: '输出目录', ready: true, detail: '已配置' }],
        });
      if (path === '/api/cloud/assets') return fulfill(route, { status: 'success', items: [] });
      if (path === '/api/cloud/rules') return fulfill(route, { status: 'success', items: [] });
      if (/\/(channels|inbox|ingestion|emby-connections)$/.test(path))
        return fulfill(route, { status: 'success', items: [], revision: 1 });
      if (path === '/api/governance/summary')
        return fulfill(route, {
          status: 'success',
          scan: { usable: false, result: { del_local_items: [], del_share_items: [] } },
        });
      if (path === '/api/plans') return fulfill(route, { status: 'success', plans: [] });
      if (path === '/api/strategy')
        return fulfill(route, { status: 'success', strategy: defaults.strategy });
      if (path === '/api/cover-strategy')
        return fulfill(route, { status: 'success', strategy: defaults.cover });
      if (path === '/api/governance/auto')
        return fulfill(route, {
          status: 'success',
          settings: {
            enabled: false,
            interval_hours: 24,
            notify_when_clean: false,
            only_on_change: true,
          },
        });
      if (path === '/api/wash/empty-dirs/browse')
        return fulfill(route, {
          status: 'success',
          path: '',
          roots: [{ path: '/media/local', name: '本地库' }],
          items: [],
          has_more: false,
        });
      if (path === '/api/logs') return fulfill(route, { status: 'success', logs: [], cursor: 0 });
      if (path === '/api/bot/status')
        return fulfill(route, { status: 'success', bot: { running: false, state: 'disabled' } });
      if (path === '/api/ingest')
        return fulfill(route, { status: 'success', movies: 0, series: 0, episodes: 0 });
      if (path === '/api/records') return fulfill(route, { status: 'success', records: [] });
      if (path === '/api/morning')
        return fulfill(route, {
          status: 'success',
          morning: { enabled: false, hour: 9, minute: 0, prescan_min: 5, items: ['stats'] },
          items: [{ key: 'stats', label: '概览统计' }],
        });
      if (path === '/api/ingest/settings')
        return fulfill(route, {
          status: 'success',
          settings: { enabled: true, mode: 'poll', interval_min: 5, reconcile_hours: 24 },
        });
      if (['/api/storage/status', '/api/library/health', '/api/consistency'].includes(path))
        return fulfill(route, { status: 'success' });
      if (path === '/api/upstream/config')
        return fulfill(route, {
          status: 'success',
          settings: { enabled: false, url: '', has_token: false, max_points: 0 },
        });
      if (path === '/api/upstream/jobs') return fulfill(route, { status: 'success', items: [] });
      if (path === '/api/config')
        return fulfill(route, {
          status: 'success',
          config: {
            emby_host: 'http://192.168.1.10:8096',
            emby_key: 'abcd****1234',
            tmdb_key: 'tmdb****1234',
            telegram_bot_token: 'bot****1234',
            telegram_chat_id: '123',
            emby_local_path: '/media/local',
            emby_share_path: '/media/share',
          },
          env_overridden: [],
        });
      if (path.startsWith('/api/config/test/'))
        return fulfill(route, { status: 'success', message: '连接成功' });
      return fulfill(route, { detail: `Unmocked: ${method} ${path}` }, 404);
    },
  );
  return state;
}
