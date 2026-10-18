import { describe, expect, it } from 'vitest';
import { badge, episodeSummary } from '../src/composables/format';
describe('pre-Vue media facts', () => {
  it('keeps uncollected, stale, ambiguous and unknown episode states distinct', () => {
    expect(badge({ type: 'tv', in_emby: false, library_status: 'available' })).toBe('未入库');
    expect(badge({ type: 'tv', library_status: 'unavailable' })).toBe('片库待同步');
    expect(badge({ type: 'tv', eps_source: 'ambiguous' })).toBe('身份冲突');
    expect(badge({ type: 'tv', in_emby: true })).toBe('集数待同步');
  });
  it('uses the backend union counts and total instead of a generic in-library flag', () => {
    expect(
      badge({ type: 'tv', in_emby: true, eps: { have: 18, total: 24, match_status: 'missing' } }),
    ).toBe('已入库 18/24 集');
    expect(badge({ type: 'tv', eps: { have: 24, total: 24, match_status: 'aligned' } })).toBe(
      '完整 · 24/24 集',
    );
    expect(
      badge({ type: 'tv', facts_stale: true, eps: { have: 25, total: 24, match_status: 'extra' } }),
    ).toBe('超出对照 · 25/24 集*');
    expect(badge({ type: 'tv', eps: { have: 8 } })).toBe('已入库 8 集');
    expect(badge({ type: 'tv', eps: { have: 0, total: 24 } })).toBe('未入库');
  });
  it('honors manual completion and preserves known zero counts', () => {
    expect(badge({ _md: true, tmdb_info: { match_status: 'missing', diff: -2 } })).toBe('完整');
    expect(
      episodeSummary({ have_eps: 0, total_episodes: 12, tmdb_info: { declared_total: 24 } }),
    ).toBe('已入库 0/24 集');
  });
});
