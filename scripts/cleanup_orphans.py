"""清理孤儿记录：台账 / 分享登记里指向「磁盘上已不存在的 STRM」的行。

先备份（把将被删除的整行导出成 JSON），再删；只删「文件确实不存在」的行。
用法：python3 cleanup_orphans.py [--apply]
"""
import json
import os
import sqlite3
import sys
import time

DB = '/data/data/state.db'
ROOT = '/strm'
OUT = '/data/cleanup'
APPLY = '--apply' in sys.argv


def main():
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')

    registry = [dict(row) for row in conn.execute('SELECT * FROM output_registry')]
    orphan_registry = [row for row in registry
                       if not os.path.isfile(os.path.join(ROOT, row['relative_path']))]

    share_rows = [dict(row) for row in conn.execute('SELECT * FROM share_strms')]
    orphan_share = [row for row in share_rows
                    if row.get('strm_path')
                    and not os.path.isfile(os.path.join(ROOT, row['strm_path']))]

    print('台账总数 %d，孤儿 %d' % (len(registry), len(orphan_registry)))
    print('分享登记 %d，孤儿 %d' % (len(share_rows), len(orphan_share)))

    os.makedirs(OUT, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dump = os.path.join(OUT, 'orphans-%s.json' % stamp)
    with open(dump, 'w', encoding='utf-8') as handle:
        json.dump({'output_registry': orphan_registry, 'share_strms': orphan_share},
                  handle, ensure_ascii=False, indent=1)
    print('已备份到', dump)

    if not APPLY:
        print('（预览模式：加 --apply 才真正删除）')
        for row in orphan_registry[:5]:
            print('  将删除台账:', row['relative_path'][:80])
        return

    with conn:
        for row in orphan_registry:
            conn.execute('DELETE FROM output_registry WHERE relative_path=? AND account_id=?',
                         (row['relative_path'], row['account_id']))
        for row in orphan_share:
            conn.execute('DELETE FROM share_strms WHERE id=?', (row['id'],))
    print('已删除台账 %d 条、分享登记 %d 条' % (len(orphan_registry), len(orphan_share)))

    left = conn.execute('SELECT count(*) FROM output_registry').fetchone()[0]
    gone = [row for row in conn.execute('SELECT relative_path FROM output_registry')
            if not os.path.isfile(os.path.join(ROOT, row['relative_path']))]
    print('清理后台账 %d 条，仍缺文件 %d 条' % (left, len(gone)))
    conn.close()


if __name__ == '__main__':
    main()
