import { describe, expect, it } from 'vitest';
import { deletionOutcome } from '../src/composables/deletionOutcome';

describe('delete progress must not claim success early', () => {
  it('keeps accepted/running as running', () => {
    expect(deletionOutcome('running', {}, true)).toBe('running');
  });
  it('requires real successful result for a done delete', () => {
    expect(deletionOutcome('done', {}, true)).toBe('needs_review');
    expect(deletionOutcome('done', { status: 'error' }, true)).toBe('failed');
    expect(deletionOutcome('done', { status: 'success' }, true)).toBe('success');
  });
  it('keeps partial successful deletion visible for verification', () => {
    expect(deletionOutcome('done', { status: 'success', failed: 2 }, true)).toBe('partial');
    expect(deletionOutcome('failed', { status: 'success' }, true)).toBe('failed');
  });
});
