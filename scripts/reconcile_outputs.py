"""三表对账（命令行版）：output_registry / owned_outputs / share_strms。

用法：
    python3 reconcile_outputs.py                     # 只看报告
    python3 reconcile_outputs.py --apply             # 执行可证明的修复（会写备份）
    python3 reconcile_outputs.py --db /data/data/state.db --root /strm --apply

容器内默认路径即 ``/data/data/state.db`` 与 ``/strm``；在宿主机跑请显式传参。
"""
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, '/app')

from strimkeep.output_reconcile import KINDS, scan  # noqa: E402
from strimkeep.output_reconcile import fix as apply_fix  # noqa: E402


class Store:
    """最小外壳：对账只用 db / root / event 三个成员。"""

    def __init__(self, db_path, root):
        self.db = sqlite3.connect(db_path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=15000')
        self.root = root

    def event(self, kind, message):
        print('[%s] %s' % (kind, message))


def main():
    parser = argparse.ArgumentParser(description='三表对账')
    parser.add_argument('--db', default='/data/data/state.db')
    parser.add_argument('--root', default='/strm')
    parser.add_argument('--apply', action='store_true', help='真正写入修复（默认只看）')
    args = parser.parse_args()

    store = Store(args.db, args.root)
    report = scan(store, args.root)
    counts = report['counts']
    print('台账 %d 条、归属 %d 条、分享登记 %d 条' %
          (counts['registry'], counts['owned'], counts['share']))
    if report['healthy']:
        print('三表一致，无需处理。')
        return
    for issue in report['issues']:
        print('- %-18s %6d 条' % (issue['kind'], issue['count']))
        for item in issue['samples'][:3]:
            print('    %s' % json.dumps(item, ensure_ascii=False)[:120])
    if not args.apply:
        print('（预览模式：加 --apply 才真正修复）')
        return

    result = apply_fix(store, args.root)
    print('已修复：删归属 %d、补归属 %d、摘台账 %d、撤销分享 %d' %
          (result['ghost_ownership'] + result['orphan_ownership'],
           result['missing_owned'], result['stale_in_registry'], result['share_missing']))
    if result.get('path_conflict'):
        print('注意：仍有 %d 条路径冲突（同路径被两套归属认领），需人工判断。'
              % result['path_conflict'])
    if result.get('backup'):
        print('备份：', result['backup'])
    after = scan(store, args.root)
    print('复查：%s' % ('一致' if after['healthy'] else
                        '仍有 %d 类不一致' % len(after['issues'])))
    assert set(after['counts']) >= set(KINDS)


if __name__ == '__main__':
    main()
