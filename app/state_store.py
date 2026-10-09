"""SQLite-only state documents. Paths identify legacy imports; no JSON is written.

Each legacy input is imported at most once, in the same transaction as its marker.
Missing files do not erase database state. Invalid inputs without a usable database
value stop migration, rather than silently resetting notification/config baselines.
"""
import contextlib
import json
import threading
import time
import sys
from pathlib import Path

try:
    from . import storage, storage_status
except ImportError:
    import storage, storage_status

_MISSING = object()
_READY = set()
_FRESH_CONFIG = set()
_REVISIONS = {}
_LOCK = threading.RLock()


class StateStoreError(OSError):
    pass


def _identity(path):
    path = Path(path)
    directory = path.parent if path.parent.name == 'state' else path.parent / 'state'
    for module_name in ('app.engine', 'engine'):
        module = sys.modules.get(module_name)
        if module is None or not hasattr(module, 'STATE_DIR'):
            continue
        if path.parent == Path(module.STATE_DIR) or path.parent == Path(module.DATA_DIR):
            directory = Path(module.STATE_DIR)
            break
    return path, directory, path.stem


@contextlib.contextmanager
def _access(path, operation):
    path, directory, name = _identity(path)
    operation += ':' + name
    try:
        with storage.state_scope(directory), storage._transaction() as con:
            yield path, name, con
        storage_status.record_success(directory, operation)
    except Exception as error:
        storage_status.record_failure(directory, operation, error)
        _, reason = storage_status._reason(error)
        raise StateStoreError('%s：%s' % (operation, reason)) from error


def _payload(con, name):
    if name.startswith('plan_'):
        row = con.execute('SELECT payload,updated_at FROM plans WHERE id=?', (name[5:],)).fetchone()
    elif name == 'subscriptions_state':
        rows = con.execute('SELECT sid,payload,updated_at FROM sub_state WHERE store=?',
                           ('subscriptions_state.json',)).fetchall()
        row = (json.dumps({sid: json.loads(p) for sid, p, _ in rows}), max(r[2] for r in rows)) if rows else None
    elif name == 'tmdb_cache':
        rows = con.execute('SELECT k,payload,ts FROM cache_entries WHERE namespace=?', (name,)).fetchall()
        row = (json.dumps({k: json.loads(p) for k, p, _ in rows}), max(r[2] for r in rows)) if rows else None
    else:
        row = con.execute('SELECT payload,updated_at FROM docs WHERE name=?', (name,)).fetchone()
    return row


def _put(con, name, value):
    now = time.time()
    if name.startswith('plan_'):
        if not isinstance(value, dict) or value.get('schema_version') != 2 or value.get('id') != name[5:]:
            raise ValueError('invalid plan')
        con.execute('INSERT OR REPLACE INTO plans(id,ts,state,rule_sig,stats,executed_at,payload,updated_at)'
                    ' VALUES(?,?,?,?,?,?,?,?)',
                    (value['id'], float(value.get('ts') or 0), value.get('state') or 'pending',
                     value.get('rule_sig') or '', json.dumps(value.get('stats') or {}),
                     value.get('executed_at'), json.dumps(value, ensure_ascii=False), now))
    elif name == 'subscriptions_state':
        if not isinstance(value, dict):
            raise ValueError('invalid subscription state')
        con.execute('DELETE FROM sub_state WHERE store=?', ('subscriptions_state.json',))
        con.executemany('INSERT INTO sub_state(store,sid,payload,updated_at) VALUES(?,?,?,?)',
                        [('subscriptions_state.json', str(k), json.dumps(v, ensure_ascii=False), now)
                         for k, v in value.items()])
        # Explicit empty is an authoritative value, separate from never imported.
        storage._write_doc(con, name, '{}', now)
    elif name == 'tmdb_cache':
        if not isinstance(value, dict):
            raise ValueError('invalid TMDB cache')
        con.execute('DELETE FROM cache_entries WHERE namespace=?', (name,))
        con.executemany('INSERT INTO cache_entries(namespace,k,payload,ts) VALUES(?,?,?,?)',
                        ((name, str(k), json.dumps(v, ensure_ascii=False), now) for k, v in value.items()))
        storage._write_doc(con, name, '{}', now)
    else:
        storage._write_doc(con, name, json.dumps(value, ensure_ascii=False), now)


def _prepare(con, path, name):
    marker = 'sqlite_import:' + name
    if con.execute('SELECT 1 FROM kv WHERE k=?', (marker,)).fetchone():
        return False
    current = _payload(con, name)
    if current is None and name in ('subscriptions_state', 'tmdb_cache'):
        current = con.execute('SELECT payload,updated_at FROM docs WHERE name=?', (name,)).fetchone()
    imported = False
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if name in ('config', 'subscriptions_state', 'manual_done', 'gov_auto') and not isinstance(value, dict):
            raise ValueError('invalid state')
        if current is None or path.stat().st_mtime > current[1]:
            _put(con, name, value)
            imported = True
    except FileNotFoundError:
        pass
    except ValueError:
        if current is None:
            raise
        json.loads(current[0])  # Only a usable DB value may rescue an invalid legacy input.
    con.execute('INSERT OR REPLACE INTO kv(k,v,ts) VALUES(?,?,?)', (marker, '1', time.time()))
    return imported


def prepare(path):
    identity = (str(_identity(path)[1]), _identity(path)[2])
    with _LOCK:
        if identity not in _READY:
            with _access(path, 'state_migration') as (p, name, con):
                _prepare(con, p, name)
            _READY.add(identity)
    return identity


def read(path, default=_MISSING):
    identity = (str(_identity(path)[1]), _identity(path)[2])
    if identity in _FRESH_CONFIG and identity not in _READY:
        if default is _MISSING:
            raise FileNotFoundError('SQLite config not present')
        return default
    prepare(path)
    with _access(path, 'state_read') as (_, name, con):
        if name == 'tmdb_cache':
            rows = con.execute('SELECT k,payload FROM cache_entries WHERE namespace=?', (name,)).fetchall()
            return {k: json.loads(p) for k, p in rows}
        row = _payload(con, name)
        if row is None and name == 'subscriptions_state':
            row = con.execute('SELECT payload,updated_at FROM docs WHERE name=?', (name,)).fetchone()
        value = json.loads(row[0]) if row else _MISSING
    if value is _MISSING:
        if default is _MISSING:
            raise FileNotFoundError('SQLite state not present')
        return default
    return value


def save(path, value):
    identity = prepare(path)
    with _LOCK:
        with _access(path, 'state_write') as (_, name, con):
            _put(con, name, value)
        _REVISIONS[identity] = _REVISIONS.get(identity, 0) + 1
    return True


def stamp(path):
    _, directory, name = _identity(path)
    identity = (str(directory), name)
    if name == 'config' and identity not in _READY:
        if identity in _FRESH_CONFIG or (not Path(path).exists() and not (directory / storage.DB_NAME).exists()):
            _FRESH_CONFIG.add(identity)
            return (identity, _REVISIONS.get(identity, 0))
    identity = prepare(path)
    return (identity, _REVISIONS.get(identity, 0))


def remove(path):
    """Explicit reset; deleting a retired JSON export is never a state reset."""
    identity = prepare(path)
    with _LOCK, _access(path, 'state_write') as (_, name, con):
        if name.startswith('plan_'):
            con.execute('DELETE FROM plans WHERE id=?', (name[5:],))
        elif name == 'subscriptions_state':
            con.execute('DELETE FROM sub_state WHERE store=?', ('subscriptions_state.json',))
        elif name == 'tmdb_cache':
            con.execute('DELETE FROM cache_entries WHERE namespace=?', (name,))
        con.execute('DELETE FROM docs WHERE name=?', (name,))
    _REVISIONS[identity] = _REVISIONS.get(identity, 0) + 1


def migrate(directory):
    directory = Path(directory)
    paths = [directory.parent / 'config.json', directory.parent / 'gov_auto.json']
    paths.extend(sorted(directory.glob('*.json')))
    # Known absent inputs are also sealed, so old files cannot resurrect deleted state.
    paths.extend(directory / (name + '.json') for name in (
        'manual_done', 'wash_residuals', 'gov_latest', 'subscriptions_state'))
    identities = []
    changed = []
    skipped = []
    imported = 0
    with _LOCK, _access(directory / 'migration.json', 'state_migration') as (_, __, con):
        # SAVEPOINT would otherwise become the outermost transaction and RELEASE
        # would commit individual documents before the full migration succeeds.
        if not con.in_transaction:
            con.execute('BEGIN')
        for path in dict.fromkeys(paths):
            _, _, name = _identity(path)
            con.execute('SAVEPOINT legacy_document')
            try:
                did_import = bool(_prepare(con, path, name))
                imported += did_import
            except (ValueError, TypeError, KeyError) as error:
                con.execute('ROLLBACK TO legacy_document')
                con.execute('RELEASE legacy_document')
                if name in ('config', 'subscriptions_state', 'gov_auto', 'manual_done'):
                    raise
                skipped.append((name, error))
                # A malformed optional cache must not block its next rebuild.
                # Seal only its legacy input; do not replace existing SQL data.
                con.execute('INSERT OR REPLACE INTO kv(k,v,ts) VALUES(?,?,?)',
                            ('sqlite_import:' + name, 'skipped_invalid', time.time()))
                identities.append((str(directory), name))
                continue
            con.execute('RELEASE legacy_document')
            identity = (str(directory), name)
            identities.append(identity)
            if did_import:
                changed.append(identity)
    _READY.update(identities)
    for identity in changed:
        _FRESH_CONFIG.discard(identity)
        _REVISIONS[identity] = _REVISIONS.get(identity, 0) + 1
    for name, error in skipped:
        storage_status.record_failure(directory, 'state_migration:' + name, error)
    return imported
