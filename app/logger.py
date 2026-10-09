"""SQLite cleanup archives. Routine subscription checks go to live logs only."""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
try:
    from . import storage as _storage
except ImportError:
    import storage as _storage
if __package__:
    from . import runtime_logs
else:
    import runtime_logs

DATA_DIR = Path(os.environ.get('AGENT_DATA', '/data'))
RECORDS_DIR = DATA_DIR / 'records'
RECORDS_FILE = RECORDS_DIR / 'audit.jsonl'
LEGACY_LOG = DATA_DIR / '媒体治理明细.log'
ARCHIVE_CATEGORIES = ('库间查重巡检', '执行跨库清理', '目录清理', '洗版残留清理', '单剧删除')


def write(category, title, details=None, rule_sig=None, extra=None):
    details = [str(d) for d in (details or [])]
    log = logging.getLogger('strimkeep.' + ('subscribe' if category == '追更' else 'governance'))
    log.info('%s · %s%s', category, title, ('\n' + '\n'.join(details)) if details else '')
    if category not in ARCHIVE_CATEGORIES: return True
    rec = {'schema_version': 2, 'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'category': str(category), 'title': str(title), 'details': details, 'rule_sig': str(rule_sig or '')}
    if extra: rec['extra'] = extra
    return bool(_storage.db_add_audit(rec))


def read_recent(limit=50):
    return _storage.db_recent_audit(max(1, min(100, int(limit))))


def read_archives(limit=50):
    with _storage._transaction() as con:
        rows = con.execute('SELECT %s FROM audit WHERE category IN (%s) ORDER BY id DESC LIMIT ?' %
                           (_storage._AUDIT_COLS, ','.join('?' for _ in ARCHIVE_CATEGORIES)),
                           ARCHIVE_CATEGORIES + (max(1, min(100, int(limit))),)).fetchall()
    return [_storage._audit_row(r) for r in rows]


def to_text(limit=50):
    return '\n'.join('[%s] 【%s】 %s\n%s' % (r['ts'], r['category'], r['title'], '\n'.join(r.get('details') or [])) for r in read_recent(limit))


def migrate_legacy():
    count = 0
    paths = sorted(RECORDS_DIR.glob('audit*.jsonl')) + ([LEGACY_LOG] if LEGACY_LOG.exists() else [])
    with _storage._transaction() as con:
        for path in paths:
            marker = 'sqlite_audit_import:' + path.name
            if con.execute('SELECT 1 FROM kv WHERE k=?', (marker,)).fetchone(): continue
            records = []
            if path == LEGACY_LOG:
                for block in path.read_text(encoding='utf-8').split('\n\n'):
                    lines = block.strip().splitlines()
                    match = re.match(r'^\[([\d\-: ]+)\]\s*【([^】]+)】\s*(.*)$', lines[0]) if lines else None
                    if match: records.append(dict(ts=match[1], category=match[2], title=match[3], details=lines[1:]))
            else:
                with path.open(encoding='utf-8') as stream:
                    for line in stream:
                        try:
                            rec = json.loads(line)
                            if not isinstance(rec, dict): raise ValueError('invalid audit row')
                            records.append(rec)
                        except ValueError:
                            # Never log from inside an import transaction (handler has its own DB write).
                            continue
            for rec in records:
                values = (str(rec.get('ts') or ''), str(rec.get('category') or ''), str(rec.get('title') or ''),
                          json.dumps(rec.get('details') or [], ensure_ascii=False), str(rec.get('rule_sig') or ''),
                          json.dumps(rec['extra'], ensure_ascii=False) if rec.get('extra') else '')
                if not con.execute('SELECT 1 FROM audit WHERE ts=? AND category=? AND title=? AND details=? AND rule_sig=? AND extra=?', values).fetchone():
                    con.execute('INSERT INTO audit(ts,category,title,details,rule_sig,extra) VALUES(?,?,?,?,?,?)', values)
                    count += 1
            con.execute("INSERT INTO kv(k,v,ts) VALUES(?,?,strftime('%s','now'))", (marker, '1'))
    return count


runtime_logs.install()
