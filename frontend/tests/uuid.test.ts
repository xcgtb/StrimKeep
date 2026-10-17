import { describe, it, expect, afterEach, vi } from 'vitest';
import { uuid } from '../src/api/domain';

// 删除/任务接口要求 32 位十六进制操作号；局域网 http 访问没有 secure context，
// crypto.randomUUID 不存在，必须回退而不是抛异常（页面会弹「页面出现异常」）。
describe('operation id generation', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('produces 32 lowercase hex characters', () => {
    expect(uuid()).toMatch(/^[a-f0-9]{32}$/);
  });

  it('falls back to getRandomValues outside a secure context', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: (array: Uint8Array) => {
        array.set(Array.from({ length: 16 }, (_, index) => index));
        return array;
      },
    });
    expect(uuid()).toBe('000102030405060708090a0b0c0d0e0f');
  });

  it('still works when the crypto object is missing entirely', () => {
    vi.stubGlobal('crypto', undefined);
    expect(uuid()).toMatch(/^[a-f0-9]{32}$/);
  });
});
