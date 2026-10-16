import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, onUnauthorized, query, request } from '../src/api/client';
import { badge, posterUrl } from '../src/composables/format';
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
  onUnauthorized(() => {});
});
afterEach(() => vi.unstubAllGlobals());
describe('existing API protocol', () => {
  it('uses the HTTPOnly session and keeps 202 receipts intact', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ job_id: 'abc', state: 'queued' }), { status: 202 }),
    );
    expect(
      await request('/api/cloud/plans/apply', { method: 'POST', body: { confirmed: true } }),
    ).toEqual({ job_id: 'abc', state: 'queued' });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fetch).mock.calls[0]?.[1]).toMatchObject({
      credentials: 'same-origin',
      method: 'POST',
      body: '{"confirmed":true}',
    });
  });
  it('expires the session on 401', async () => {
    const expire = vi.fn();
    onUnauthorized(expire);
    vi.mocked(fetch).mockResolvedValue(new Response('{"detail":"请重新登录"}', { status: 401 }));
    await expect(request('/api/runtime/status')).rejects.toMatchObject({ status: 401 });
    expect(expire).toHaveBeenCalledOnce();
  });
  it.each([403, 409, 429, 500, 502])(
    'surfaces HTTP %d without replaying the request',
    async (status) => {
      vi.mocked(fetch).mockResolvedValue(new Response('{"detail":"操作未完成"}', { status }));
      await expect(request('/api/cloud/plans/apply', { method: 'POST' })).rejects.toMatchObject({
        status,
        uncertain: status >= 500,
      });
      expect(fetch).toHaveBeenCalledOnce();
    },
  );
  it('does not automatically replay a mutation after a network failure', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(request('/api/config', { method: 'POST' })).rejects.toMatchObject({
      uncertain: true,
    });
    expect(fetch).toHaveBeenCalledOnce();
  });
  it('rejects a 200 response carrying a backend error', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('{"status":"error","message":"TMDB 未配置"}'));
    await expect(request('/api/explore')).rejects.toThrow('TMDB 未配置');
  });
  it('treats an invalid mutation response as uncertain', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('<html>502</html>', { status: 502 }));
    await expect(request('/api/config', { method: 'POST' })).rejects.toBeInstanceOf(ApiError);
    expect(fetch).toHaveBeenCalledOnce();
  });
  it('encodes user input as query data', () =>
    expect(query('/api/search', { q: '片名&scope=local', page: 1 })).toBe(
      '/api/search?q=%E7%89%87%E5%90%8D%26scope%3Dlocal&page=1',
    ));
});
describe('honest media facts', () => {
  it('does not turn unknown library state into confirmed ingestion', () => {
    expect(badge({ library_status: 'unavailable', in_emby: false })).toBe('片库待同步');
    expect(badge({ in_local: true })).toBe('已有 STRM');
    expect(badge({ in_emby: true, library_status: 'available' })).toBe('已入库');
  });
  it('uses authenticated same-origin poster endpoints', () => {
    expect(posterUrl({ poster_tmdb: '/abc.jpg' })).toBe('/api/tmdb/poster/abc.jpg');
    expect(posterUrl({ poster: 'https://image.tmdb.org/t/p/w500/abc.jpg' })).toBe(
      '/api/tmdb/poster/abc.jpg',
    );
    expect(posterUrl({ poster: 'javascript:alert(1)' })).toBe('');
    expect(posterUrl({ has_image: true, id: 'a/b' })).toBe('/api/emby/poster/a%2Fb');
  });
});
