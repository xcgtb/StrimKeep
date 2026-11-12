"""孤儿侧车清理：默认只出清单（dry-run），加 --apply 才真的删。

判定规则与 `/metrics` 里的 `strimkeep_ledger_drift{kind="orphan_sidecar"}` 完全一致
（共用 `strimkeep.ledger_audit.orphans()`），所以"指标说 128、脚本删 130"不会发生。

三道保险：
1. 只删**本项目生成**的侧车：nfo/json 必须带 `<tmdbid>`/`playback_id`/`"version"`；
   字幕按目录归属（随片附件）；海报/缩略图/xml 一律不碰；
2. 只删超过 7 天的（保护窗口，避免撞上正在跑的整理任务）；
3. 默认 dry-run，`--apply` 才动文件，并逐条打印。

用法（容器内，环境变量已就位）：
    docker exec strimkeep python3 /tmp/purge_orphan_sidecars.py
    docker exec strimkeep python3 /tmp/purge_orphan_sidecars.py --apply
"""
import argparse
import collections
import glob
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get('STRIMKEEP_BACKEND', '/app'))

from strimkeep.ledger_audit import LIBRARY_DIRS, orphans  # noqa: E402


def find_db(data_dir):
    path = Path(data_dir) / 'data' / 'state.db'
    if path.is_file():
        return str(path)
    hits = sorted(glob.glob(str(Path(data_dir) / '*' / 'state.db')))
    if hits:
        return hits[0]
    raise SystemExit('找不到 state.db（用 STRIMKEEP_DATA_DIR 指定数据目录）')


class Store:
    """只要一个 .db，够 ledger_audit 用。"""

    def __init__(self, db):
        self.db = db


def prune_empty_dirs(path, root):
    """删完文件后清理空目录；库根永远不动。"""
    removed = 0
    current = Path(path).parent
    root = Path(root)
    while current != root and current.parent != root and root in current.parents:
        if current.name in LIBRARY_DIRS:
            break
        try:
            if any(current.iterdir()):
                break
            current.rmdir()
            removed += 1
        except OSError:
            break
        current = current.parent
    return removed


def main(argv):
    parser = argparse.ArgumentParser(description='清理没有对应 STRM/视频的孤儿侧车')
    parser.add_argument('--apply', action='store_true', help='真的删除（默认只出清单）')
    parser.add_argument('--data', default=os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
    parser.add_argument('--strm', default=os.environ.get('STRIMKEEP_STRM_DIR', '/strm'))
    parser.add_argument('--min-age-days', type=float, default=7.0,
                        help='保护窗口，默认 7 天')
    args = parser.parse_args(argv)

    db = sqlite3.connect(find_db(args.data))
    found = orphans(Store(db), args.strm, min_age=args.min_age_days * 86400)
    deletable = [item for item in found if item['deletable']]
    too_new = [item for item in found if item['owned'] and not item['deletable']]
    unknown = [item for item in found if not item['owned']]

    print('孤儿侧车：%d 条' % len(found))
    print('  可删除：%d（归属可确认且超过 %.0f 天）' % (len(deletable), args.min_age_days))
    print('  跳过：%d（太新 %d / 归属不明 %d）' % (len(too_new) + len(unknown),
                                                 len(too_new), len(unknown)))
    for label, group in (('按类型', collections.Counter(i['suffix'] for i in deletable)),
                         ('按库', collections.Counter(i['relative'].split('/')[0]
                                                   for i in deletable))):
        print('  %s：%s' % (label, dict(group)))
    print()
    for item in deletable[:15]:
        print('   %-6s %5.1f天  %s' % (item['suffix'], item['age_days'], item['relative'][-88:]))
    if len(deletable) > 15:
        print('   …… 另有 %d 条' % (len(deletable) - 15))

    if not args.apply:
        print()
        print('dry-run：没有删除任何文件（加 --apply 才删）')
        return 0

    root = Path(args.strm)
    deleted = dirs = failed = 0
    for item in deletable:
        path = root / item['relative']
        try:
            path.unlink()
            deleted += 1
            dirs += prune_empty_dirs(path, root)
        except OSError as exc:
            failed += 1
            print('   删除失败：%s（%s）' % (item['relative'], exc))
    detail = ', '.join('%s %d' % (k, v) for k, v in
                       collections.Counter(i['suffix'] for i in deletable).items())
    with db:
        db.execute("INSERT INTO events(at,kind,message) VALUES(?,?,?)",
                   (time.time(), 'system',
                    '孤儿侧车清理：删除 %d 个（%s），清理空目录 %d 个，失败 %d 个。'
                    % (deleted, detail, dirs, failed)))
    print()
    print('已删除 %d 个文件，清理空目录 %d 个，失败 %d 个。' % (deleted, dirs, failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
