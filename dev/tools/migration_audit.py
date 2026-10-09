#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only inventory of legacy *.migrated state files.

No moves, deletes, backups, checkpoint, VACUUM, or application imports.
The optional SQLite lookup uses a read-only connection and is advisory: missing
plan rows are NOT an instruction to remove legacy plan files.
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path


def _plan_ids(state: Path):
    db = state / 'strimkeep.db'
    if not db.is_file() or db.is_symlink():
        return None, 'SQLite 主库不可用，无法核查历史计划'
    try:
        con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
        try:
            con.execute('PRAGMA query_only=ON')
            ids = {str(r[0]) for r in con.execute('SELECT id FROM plans')}
            return ids, ''
        finally:
            con.close()
    except sqlite3.Error as exc:
        return None, 'SQLite 只读查询失败: ' + str(exc)


def inspect(state: Path):
    state = Path(state)
    if not state.is_dir() or state.is_symlink():
        raise ValueError('必须传入真实存在且非符号链接的 state 目录')
    plans, db_error = _plan_ids(state)
    report = {
        'directory': str(state), 'readonly': True, 'database_error': db_error,
        'total': 0, 'bytes': 0, 'plans': {'total': 0, 'in_db': 0, 'not_in_db': 0, 'db_unknown': 0},
        'others': {'total': 0, 'has_current_file': 0, 'missing_current_file': 0},
        'largest': [], 'warning': '仅供识别和制定备份计划；任何类别均不等于可直接删除。',
    }
    largest = []
    for directory, dirs, files in os.walk(state, followlinks=False):
        parent = Path(directory)
        dirs[:] = [d for d in dirs if not (parent / d).is_symlink()]
        for name in files:
            if not name.endswith('.migrated'):
                continue
            p = parent / name
            if p.is_symlink() or not p.is_file():
                continue
            n = p.stat().st_size
            report['total'] += 1
            report['bytes'] += n
            if name.startswith('plan_') and name.endswith('.json.migrated'):
                plan_id = name[len('plan_'):-len('.json.migrated')]
                report['plans']['total'] += 1
                if plans is None:
                    report['plans']['db_unknown'] += 1
                elif plan_id in plans:
                    report['plans']['in_db'] += 1
                else:
                    report['plans']['not_in_db'] += 1
                cls = 'history_plan'
            else:
                report['others']['total'] += 1
                current = p.with_name(name[:-len('.migrated')])
                if current.is_file() and not current.is_symlink():
                    report['others']['has_current_file'] += 1
                else:
                    report['others']['missing_current_file'] += 1
                cls = 'other_migration'
            largest.append({'name': str(p.relative_to(state)), 'bytes': n, 'kind': cls})
    report['largest'] = sorted(largest, key=lambda e: (-e['bytes'], e['name']))[:10]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读核查 *.migrated 历史副本，不执行清理')
    parser.add_argument('state', nargs='?', default='/data/state')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = inspect(Path(args.state))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print('历史迁移副本核查（只读）:', report['directory'])
    print('副本: %d 个 / %.2f MiB' % (report['total'], report['bytes'] / 1048576))
    p = report['plans']
    print('历史治理计划: %d 个；SQLite 已收录 %d，未收录 %d，未知 %d' % (
        p['total'], p['in_db'], p['not_in_db'], p['db_unknown']))
    o = report['others']
    print('其他迁移副本: %d 个；同名现行文件存在 %d，不存在 %d' % (
        o['total'], o['has_current_file'], o['missing_current_file']))
    if report['database_error']:
        print('提示:', report['database_error'])
    print('大文件（最多 10 项）:')
    for item in report['largest']:
        print('  %.2f MiB  %s' % (item['bytes'] / 1048576, item['name']))
    print('注意:', report['warning'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
