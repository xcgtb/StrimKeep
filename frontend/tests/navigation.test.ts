import { describe, it, expect } from 'vitest';
import {
  navigation,
  allNavigation,
  matchesNavigation,
  searchFunctions,
  moduleName,
} from '../src/config/navigation';
describe('workspace navigation contracts', () => {
  it('keeps ten primary modules and only the drive expandable', () => {
    expect(navigation.map((n) => n.group)).toEqual([
      '媒体总览',
      '我的网盘',
      '影视探索',
      '片库映射',
      '双库治理',
      '日志中心',
      '追更订阅',
      '入库记录',
      '系统状态',
      '全局设置',
    ]);
    expect(navigation.filter((n) => !n.directTo).map((n) => n.group)).toEqual(['我的网盘']);
    expect(navigation[1]!.items.some((n) => n.to === '/organize/quality')).toBe(false);
    expect(new Set(allNavigation.map((n) => n.to)).size).toBe(allNavigation.length);
  });
  it('resolves query destinations even with extra parameters or no tab', () => {
    expect(matchesNavigation('/resources/receive?tab=batch', '/resources/receive', {})).toBe(true);
    expect(
      matchesNavigation('/resources/receive?tab=offline', '/resources/receive', {
        tab: 'offline',
        from: 'bot',
      }),
    ).toBe(true);
    expect(matchesNavigation('/resources/receive?tab=offline', '/resources/receive', {})).toBe(
      false,
    );
  });
  it.each(['免转存', '八维', '模板', '磁力', 'ed2k', '机器人', '入库', '七维', '自动整理'])(
    'finds %s without adding primary menu rows',
    (word) => {
      expect(searchFunctions(word).length).toBeGreaterThan(0);
    },
  );
  it('maps settings back to the owning workspace', () => {
    expect(moduleName('/organize')).toBe('organize');
    expect(moduleName('/settings/cloud', 'filters')).toBe('organize');
    expect(moduleName('/governance/rules')).toBe('governance');
    expect(moduleName('/tasks/ingestion')).toBe('ingestion');
  });
});
