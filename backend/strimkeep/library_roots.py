"""本地 STRM 库根的对照检查与改名（0.8.30）。

STRM 是 115 目录的映射，所以「本地第一段目录名」必须和网盘上的目录名一致：本地树、
台账 `relative_path`、Emby 库根、磁盘上的真实目录，四处同名才不会打架。

2026-11 的现场就是这样崩的：配置里写的是「影视媒体库」，115 上的归档目录叫「媒体库」，
于是整理/同步按配置把**台账**改到「影视媒体库」，文件却还留在「媒体库」——
978 行台账里 924 行在磁盘上找不到，Emby 只能用其中一边。

本模块只做两件事：

- `inspect_state()`：只读对照（配置 / 台账 / 磁盘），给 CLI 与启动自检用；
- `apply_rename()`：把库根从 old 改成 new —— 先移动文件、再重写路径列，全过程记录在
  meta 里，可以用 `rollback()` 反向撤销。

CLI（容器内，建议先 `docker compose stop strimkeep` 再跑）：

    python -m strimkeep.library_roots --state
    python -m strimkeep.library_roots --rename 影视媒体库 媒体库 --dry-run
    python -m strimkeep.library_roots --rename 影视媒体库 媒体库 --apply
    python -m strimkeep.library_roots --set-config ''        # 留空 = 镜像 115 目录名
    python -m strimkeep.library_roots --rollback
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path

# 所有可能出现相对路径的表 / 列。JSON 列里嵌着路径（例如 plan.local_files 的键）。
PATH_COLUMNS = {
    'playbacks': ['relative_path'],
    'organized_files': ['relative_path'],
    'output_registry': ['relative_path', 'files'],
    'owned_outputs': ['relative_path'],
    'organize_items': ['plan', 'original'],
    'organize_jobs': ['config'],
    'emby_deliveries': ['payload'],
}
JSON_COLUMNS = {'plan', 'original', 'config', 'files', 'payload'}
MIGRATION_KEY = 'library_root_migration'
NOTICE_KEY = 'library_root_notice'


def rewrite(value, old, new):
    """把 old 这一级目录名改成 new（字符串、列表、字典都递归处理）。"""
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old + '/'):
            return new + value[len(old):]
        return value.replace('/' + old + '/', '/' + new + '/')
    if isinstance(value, list):
        return [rewrite(item, old, new) for item in value]
    if isinstance(value, dict):
        return {rewrite(key, old, new): rewrite(item, old, new) for key, item in value.items()}
    return value


def _segment_sql(table):
    return ("SELECT substr(relative_path,1,instr(relative_path,'/')-1) seg,count(*) c "
            'FROM %s GROUP BY seg ORDER BY c DESC' % table)


def ledger_roots(store):
    """台账里各表的「第一段目录名 → 行数」。"""
    result = {}
    for table in PATH_COLUMNS:
        try:
            result[table] = {str(row[0]): int(row[1])
                             for row in store.db.execute(_segment_sql(table))}
        except sqlite3.Error:
            result[table] = {}
    return result


def disk_roots(root):
    """磁盘上每个一级目录的 strm / 文件数。"""
    base = Path(root)
    out = []
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        strm = files = 0
        for _dirpath, _dirnames, filenames in os.walk(entry):
            for name in filenames:
                files += 1
                if name.lower().endswith('.strm'):
                    strm += 1
        out.append({'name': entry.name, 'strm': strm, 'files': files})
    return out


def inspect_state(store, root):
    """配置 / 台账 / 磁盘三方对照（只读）。"""
    from .organize_options import library_root
    config = store.get('organize_config', {}) or {}
    return {'library_dir_name': config.get('library_dir_name') or '',
            'expected_root': library_root(config),
            'archive_cid': config.get('archive_cid') or '',
            'disk': disk_roots(root),
            'ledger': ledger_roots(store)}

def plan_rename(store, root, old, new):
    """演练：要移动哪些文件、哪些冲突、要改多少行。"""
    base = Path(root)
    old_dir, new_dir = base / old, base / new
    moves, collisions = [], []
    if old_dir.is_dir() and not old_dir.is_symlink():
        for dirpath, _dirnames, filenames in os.walk(old_dir):
            for name in sorted(filenames):
                source = Path(dirpath) / name
                relative = source.relative_to(old_dir).as_posix()
                target = new_dir / relative
                (collisions if target.exists() else moves).append(relative)
    rows = {}
    for table, columns in PATH_COLUMNS.items():
        count = 0
        try:
            for row in store.db.execute('SELECT rowid AS rid,* FROM %s' % table).fetchall():
                for column in columns:
                    if column not in row.keys() or not isinstance(row[column], str):
                        continue
                    raw = row[column]
                    if column in JSON_COLUMNS:
                        try:
                            if not _mentions(json.loads(raw), old):
                                continue
                        except ValueError:
                            continue
                    elif old not in raw:
                        continue
                    count += 1
                    break
        except sqlite3.Error:
            continue
        rows[table] = count
    return {'old': old, 'new': new, 'old_dir': str(old_dir), 'new_dir': str(new_dir),
            'old_exists': old_dir.exists(), 'new_exists': new_dir.exists(),
            'moves': moves, 'collisions': collisions, 'rows': rows}


def _remove_empty(directory, stop):
    """自底向上删空目录，但绝不删除 stop（库根本身）。"""
    directory, stop = Path(directory), Path(stop)
    for dirpath, _dirnames, _filenames in sorted(os.walk(directory), key=lambda item: -len(item[0])):
        current = Path(dirpath)
        if current == stop:
            continue
        try:
            current.rmdir()
        except OSError:
            pass


def _mentions(value, name):
    """JSON 里是否真的出现了这个目录名（不能靠字符串匹配：非 ASCII 可能被转义成 \\uXXXX）。"""
    if isinstance(value, str):
        return value == name or name in value
    if isinstance(value, list):
        return any(_mentions(item, name) for item in value)
    if isinstance(value, dict):
        return any(_mentions(key, name) or _mentions(item, name) for key, item in value.items())
    return False


def _rewrite_rows(store, old, new):
    """在一个事务里改完所有路径列，返回 {表: 改动行数}。"""
    changed = {}
    with store.db:
        for table, columns in PATH_COLUMNS.items():
            rows = store.db.execute('SELECT rowid AS rid,* FROM %s' % table).fetchall()
            count = 0
            for row in rows:
                updates = {}
                for column in columns:
                    if column not in row.keys():
                        continue
                    raw = row[column]
                    if not isinstance(raw, str):
                        continue
                    if column in JSON_COLUMNS:
                        # JSON 列必须先解析再判断：`json.dumps` 默认把中文转义成 \uXXXX，
                        # 拿 old 去做子串匹配会一条都匹配不到（现场验证过）。
                        try:
                            parsed = json.loads(raw)
                        except ValueError:
                            continue
                        if not _mentions(parsed, old):
                            continue
                        value = json.dumps(rewrite(parsed, old, new), ensure_ascii=False)
                    else:
                        if old not in raw:
                            continue
                        value = rewrite(raw, old, new)
                    if value != raw:
                        updates[column] = value
                if updates:
                    assignments = ','.join('%s=?' % column for column in updates)
                    store.db.execute('UPDATE OR REPLACE %s SET %s WHERE rowid=?' % (table, assignments),
                                     (*updates.values(), row['rid']))
                    count += 1
            changed[table] = count
    return changed



def apply_rename(store, root, old, new, dry_run=True):
    """把库根 old 改成 new。有冲突就整体拒绝（绝不覆盖任何已存在的文件）。"""
    old, new = str(old or '').strip(), str(new or '').strip()
    if not old or not new:
        return {'status': 'invalid', 'reason': '新旧目录名都不能为空'}
    if old == new:
        return {'status': 'noop', 'reason': '新旧目录名相同'}
    plan = plan_rename(store, root, old, new)
    if plan['collisions']:
        return {'status': 'blocked', 'reason': '目标目录已有同名文件，先人工处理再执行',
                'moves': len(plan['moves']), 'rows': plan['rows'],
                'collisions': plan['collisions'][:20]}
    if dry_run:
        return {'status': 'dry_run', 'old': old, 'new': new, 'old_exists': plan['old_exists'],
                'new_exists': plan['new_exists'], 'moves': len(plan['moves']),
                'sample': plan['moves'][:5], 'rows': plan['rows']}

    base = Path(root)
    old_dir, new_dir = base / old, base / new
    moved = 0
    # 0.8.32：改名同样会让 Emby 把旧路径当成删除；登记后深删不会去删云端。
    from .internal_delete import remember
    if not remember(store, [old + '/' + relative for relative in plan['moves']]):
        return {'status': 'blocked', 'reason': '内部删除登记失败，已保留全部文件。'}
    for relative in plan['moves']:
        source, target = old_dir / relative, new_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        moved += 1
    if old_dir.is_dir():
        _remove_empty(old_dir, base)
    rows = _rewrite_rows(store, old, new)
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         (MIGRATION_KEY, json.dumps({'old': old, 'new': new, 'at': time.time(),
                                                     'moves': plan['moves']}, ensure_ascii=False)))
        store.db.execute('DELETE FROM sync_snapshots')
        store.db.execute('DELETE FROM meta WHERE key=?', (NOTICE_KEY,))
    store.event('system',
                '库根已从「%s」改为「%s」：移动 %d 个文件、重写 %d 行路径；'
                'Emby 的库路径需要指向 /<挂载点>/%s。'
                % (old, new, moved, sum(rows.values()), new))
    return {'status': 'applied', 'old': old, 'new': new, 'moved': moved, 'rows': rows}


def rollback(store, root):
    """撤销上一次 apply_rename()：文件搬回去、路径改回去。"""
    raw = store.meta(MIGRATION_KEY)
    if not raw:
        return {'status': 'nothing', 'reason': '没有可回滚的库根改动'}
    manifest = json.loads(raw)
    old, new = manifest['old'], manifest['new']
    base = Path(root)
    old_dir, new_dir = base / old, base / new
    moved = 0
    # Rollback removes the new paths as well, so protect them before any move.
    from .internal_delete import remember
    if not remember(store, [new + '/' + relative for relative in (manifest.get('moves') or [])]):
        return {'status': 'blocked', 'reason': '内部删除登记失败，已保留全部文件。'}
    for relative in reversed(manifest.get('moves') or []):
        source, target = new_dir / relative, old_dir / relative
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            moved += 1
    if new_dir.is_dir():
        _remove_empty(new_dir, base)
    rows = _rewrite_rows(store, new, old)
    with store.db:
        store.db.execute('DELETE FROM meta WHERE key=?', (MIGRATION_KEY,))
    store.event('system', '库根改动已回滚：%s → %s（移动 %d 个文件、重写 %d 行路径）。'
                % (new, old, moved, sum(rows.values())))
    return {'status': 'rolled_back', 'from': new, 'to': old, 'moved': moved, 'rows': rows}


def startup_check(store, root):
    """配置的库根和磁盘上的实际库根不一致时报警一次（只报警，不动文件）。"""
    try:
        state = inspect_state(store, root)
    except Exception:                                  # noqa: BLE001 - 自检绝不阻塞启动
        return None
    names = sorted({entry['name'] for entry in state['disk'] if entry['strm']})
    expected = state['expected_root']
    if not names or expected in names:
        with store.db:
            store.db.execute('DELETE FROM meta WHERE key=?', (NOTICE_KEY,))
        return None
    signature = '%s|%s' % (expected, ','.join(names))
    if store.meta(NOTICE_KEY) == signature:
        return None
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (NOTICE_KEY, signature))
    store.event('system',
                '库根不一致：配置（或 115 目录名）是「%s」，磁盘上的库目录是 %s。'
                'STRM 会写进「%s」，Emby 可能收录不到；核对命令：docker compose run --rm '
                '--entrypoint python strimkeep -m strimkeep.library_roots --state'
                % (expected, '、'.join('「%s」' % name for name in names), expected))
    return signature


EPISODE = re.compile(r'S(\d{1,2})E(\d{1,3})')


def _work_and_episode(relative):
    """把 STRM 相对路径拆成 (作品目录, 集标识)；电影用 'MOVIE'。"""
    parts = relative.split('/')
    for index, part in enumerate(parts):
        if part.startswith('Season ') and index + 1 < len(parts):
            match = EPISODE.search(parts[-1])
            key = 'S%02dE%03d' % (int(match.group(1)), int(match.group(2))) if match else parts[-1]
            return '/'.join(parts[:index]), key
    return '/'.join(parts[:-1]), 'MOVIE'


def legacy_report(store, root, library=None):
    """列出「库里没登记、但同一集/同一部电影已有登记路径」的旧命名副本（只读）。

    判定口径与现场审计一致：
    - 登记集合 = `organized_files` ∪ `output_registry` 的 relative_path；
    - 有一条未登记的同集/同电影文件，且登记那份在磁盘上确实存在 → 这条是副本，可删；
    - 整部作品没有任何有效登记（只有旧命名）→ 保留（删了就少集）；
    - 同一作品但这一集没有登记 → 保留（旧文件是唯一来源）。
    """
    from .organize_options import library_root
    base = Path(root)
    library = library or library_root(store.get('organize_config', {}) or {})
    known = {row[0] for row in store.db.execute('SELECT relative_path FROM organized_files')}
    known |= {row[0] for row in store.db.execute('SELECT relative_path FROM output_registry')}
    covered = {_work_and_episode(relative) for relative in known if (base / relative).is_file()}
    deletable, keep = [], []
    area = base / library
    if area.is_dir():
        for dirpath, _dirnames, filenames in os.walk(area):
            for name in filenames:
                if not name.lower().endswith('.strm'):
                    continue
                relative = (Path(dirpath) / name).relative_to(base).as_posix()
                if relative in known:
                    continue
                work, episode = _work_and_episode(relative)
                entry = {'relative_path': relative, 'work': work, 'episode': episode}
                (deletable if (work, episode) in covered else keep).append(entry)
    keeps = {}
    for entry in keep:
        keeps.setdefault(entry['work'], set()).add(entry['episode'])
    return {'library': library, 'deletable': deletable, 'keep': keep,
            'keep_works': {work: sorted(keys) for work, keys in keeps.items()},
            'counts': {'registered': len(known), 'deletable': len(deletable), 'keep': len(keep)}}


def prune_legacy(store, root, apply=False, backup_dir=None):
    """删除旧命名副本。默认只演练；`apply=True` 时先写清单（含原文）再删。"""
    from .fsops import purge_strm
    report = legacy_report(store, root)
    if not apply:
        return {'status': 'dry_run', 'library': report['library'], **report['counts'],
                'sample': [entry['relative_path'] for entry in report['deletable'][:10]],
                'kept_works': {work: keys[:8] for work, keys in report['keep_works'].items()}}
    # 0.8.32：深删是真删模式时拒绝执行——删本地 STRM 会让 Emby 报删除，进而删云端源文件。
    config = store.get('sync_config', {}) or {}
    if not bool(config.get('deep_delete_dry_run', True)):
        return {'status': 'blocked',
                'reason': '深度删除当前是「真删」模式：删本地 STRM 会让 Emby 报删除并触发删除云端源文件。'
                          '请先把 Emby 深度删除改为演练（或先跑不带 --apply 的演练），再执行本命令。'}
    base = Path(root)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    folder = Path(backup_dir) if backup_dir else store.path.parent
    manifest = folder / ('prune-legacy-%s.json' % stamp)
    payload = {'at': time.time(), 'library': report['library'], 'files': [
        {'relative_path': entry['relative_path'],
         'content': (base / entry['relative_path']).read_text('utf-8', 'ignore')}
        for entry in report['deletable']]}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    removed = sidecars = dirs = 0
    from .internal_delete import remember
    remember(store, [entry['relative_path'] for entry in report['deletable']])
    for entry in report['deletable']:
        outcome = purge_strm(base / entry['relative_path'], base, store=store)
        removed += outcome['file']
        sidecars += outcome['sidecars']
        dirs += outcome['dirs']
    store.event('system', '旧命名副本清理：删除 %d 个 STRM（附属文件 %d、空目录 %d），清单 %s。'
                % (removed, sidecars, dirs, manifest.name))
    return {'status': 'applied', 'removed': removed, 'sidecars': sidecars, 'dirs': dirs,
            'manifest': str(manifest), 'kept': report['counts']['keep']}


def restore_legacy(store, root, manifest):
    """按清单把删掉的 STRM 原样写回去。"""
    data = json.loads(Path(manifest).read_text(encoding='utf-8'))
    base = Path(root)
    restored = 0
    for entry in data.get('files', []):
        path = base / entry['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry['content'], encoding='utf-8')
        restored += 1
    store.event('system', '旧命名副本回滚：从 %s 恢复 %d 个 STRM。'
                % (Path(manifest).name, restored))
    return {'status': 'restored', 'restored': restored}


def main(argv=None):
    """CLI：对照 / 改名 / 设置配置 / 回滚 / 清理旧命名副本。"""
    parser = argparse.ArgumentParser(description='本地 STRM 库根对照与改名（0.8.30+）')
    parser.add_argument('--state', action='store_true', help='只读打印配置/台账/磁盘对照')
    parser.add_argument('--rename', nargs=2, metavar=('OLD', 'NEW'), help='把库根 OLD 改成 NEW')
    parser.add_argument('--apply', action='store_true', help='真正执行（默认只演练）')
    parser.add_argument('--rollback', action='store_true', help='回滚上一次改名')
    parser.add_argument('--set-config', metavar='NAME',
                        help="设置 library_dir_name；传空字符串表示镜像 115 目录名")
    parser.add_argument('--prune-legacy', action='store_true',
                        help='清理旧命名副本（只删「同一集已有登记路径」的，默认演练）')
    parser.add_argument('--restore', metavar='MANIFEST', help='按清理清单恢复被删的旧命名副本')
    parser.add_argument('--backup-dir', default=None, help='清理清单写到哪个目录（默认数据目录）')
    parser.add_argument('--data', default=os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
    parser.add_argument('--strm', default=os.environ.get('STRIMKEEP_STRM_DIR', '/strm'))
    args = parser.parse_args(argv)

    from .organize_options import OrganizeOptions
    from .store import Store
    store = Store(Path(args.data), '', '')
    try:
        if args.set_config is not None:
            # 只改这一个键，其余（archive_cid / source_cids / directory_labels / 通知开关…）
            # 原样保留。这里**不能**用 options() 重建配置：那是只含整理选项的模型，
            # 会把不在模型里的键整体丢掉（0.8.30 现场踩过：archive_cid 被清空）。
            config = dict(store.get('organize_config', {}) or {})
            config['library_dir_name'] = str(args.set_config or '').strip()
            OrganizeOptions.model_validate(config)
            store.set_many({'organize_config': config})
            store.event('organize', '库根名已设为「%s」。'
                        % (config['library_dir_name'] or '镜像 115 目录名'))
            print('library_dir_name =', repr(config['library_dir_name']))
            print('保留的其它字段:', ','.join(sorted(key for key in config if key != 'library_dir_name')))
        if args.rename:
            print(json.dumps(apply_rename(store, Path(args.strm), *args.rename, dry_run=not args.apply),
                             ensure_ascii=False, indent=2))
        if args.rollback:
            print(json.dumps(rollback(store, Path(args.strm)), ensure_ascii=False, indent=2))
        if args.prune_legacy:
            print(json.dumps(prune_legacy(store, Path(args.strm), apply=args.apply,
                                          backup_dir=args.backup_dir), ensure_ascii=False, indent=2))
        if args.restore:
            print(json.dumps(restore_legacy(store, Path(args.strm), args.restore),
                             ensure_ascii=False, indent=2))
        if args.state or not (args.rename or args.rollback or args.prune_legacy or args.restore
                             or args.set_config is not None):
            print(json.dumps(inspect_state(store, Path(args.strm)), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0



if __name__ == '__main__':
    raise SystemExit(main())

