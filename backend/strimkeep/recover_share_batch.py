"""Dry-run first: restore exactly one confirmed share deep-delete batch."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from .share_service import PLAYABLE, share_play_url


class RecoveryError(Exception):
    pass


def connect(data, apply=False):
    uri = (Path(data) / 'data/state.db').resolve().as_uri()
    db = sqlite3.connect(uri + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def playback_base(db, data):
    row = db.execute("SELECT value FROM settings WHERE key='playback_base'").fetchone()
    try:
        value = json.loads(Fernet((Path(data) / 'config/master.key').read_bytes().strip()).decrypt(row['value'].encode()))
        parsed = urlsplit(value)
    except Exception:
        raise RecoveryError('无法读取播放地址') from None
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
        raise RecoveryError('播放地址无效')
    return value


def entries(db, batch, count):
    rows = db.execute('SELECT * FROM deep_delete_queue WHERE batch=? ORDER BY id', (batch,)).fetchall()
    if len(rows) != count or count <= 0:
        raise RecoveryError('批次数量不符')
    if any(row['state'] != 'done' or row['kind'] != 'share' or row['dry_run'] != 0 or not row['share_id'] for row in rows):
        raise RecoveryError('批次包含非分享、未完成或预演记录')
    if len({r['share_id'] for r in rows}) != count or len({r['relative_path'] for r in rows}) != count:
        raise RecoveryError('批次身份或路径重复')
    return rows


def parent_fd(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or rel.parts[0] != '分享影视库' or rel.suffix.lower() != '.strm':
        raise RecoveryError('目标路径不安全')
    if any(part in ('.', '..') or chr(92) in part or chr(0) in part for part in rel.parts):
        raise RecoveryError('目标路径不安全')
    fd = None
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in rel.parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd, rel.name
    except OSError:
        if fd is not None: os.close(fd)
        raise RecoveryError('父目录不存在或含符号链接') from None


def read_target(fd, name):
    try: file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError: return None
    except OSError: raise RecoveryError('目标存在或不安全') from None
    try:
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096: raise RecoveryError('目标不是安全的普通文件')
        return os.read(file, 4097)
    finally: os.close(file)


def source_extension(db, row):
    extensions = set()
    for item in db.execute('SELECT original,plan FROM organize_items WHERE file_id=? ORDER BY id DESC', (row['file_id'],)):
        try:
            original, plan = json.loads(item['original']), json.loads(item['plan'])
            if plan.get('playback_id') != 'share/' + row['id'] or str(original.get('id')) != row['file_id']: continue
            ext = Path(original.get('name') or '').suffix.lower()
            if ext in PLAYABLE: extensions.add(ext)
        except (TypeError, ValueError): continue
    if len(extensions) != 1: raise RecoveryError('视频扩展名缺失或有歧义')
    return next(iter(extensions))


def intent_for(db, batch, sid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='share_recovery_intents'").fetchone(): return None
    return db.execute('SELECT * FROM share_recovery_intents WHERE batch=? AND share_id=?', (batch, sid)).fetchone()


def verify(db, root, base, batch, item):
    live = db.execute('SELECT * FROM deep_delete_queue WHERE id=?', (item['id'],)).fetchone()
    if not live or any(live[k] != item[k] for k in ('batch', 'kind', 'share_id', 'relative_path', 'state', 'dry_run')):
        raise RecoveryError('深删批次记录发生变化')
    row = db.execute('SELECT * FROM share_strms WHERE id=?', (item['share_id'],)).fetchone()
    if not row or not re.fullmatch(r'[0-9a-f]{32}', row['id']) or not row['share_code'] or not row['file_id']:
        raise RecoveryError('分享身份不完整')
    if row['strm_path'] != item['relative_path'] or row['token_hash'] != hashlib.sha256(b'').hexdigest():
        raise RecoveryError('登记路径或令牌已变化')
    if db.execute('SELECT 1 FROM share_revocations WHERE share_code=? AND file_id=?', (row['share_code'], row['file_id'])).fetchone():
        raise RecoveryError('分享已主动撤销')
    if db.execute('SELECT 1 FROM share_strms WHERE id<>? AND revoked=0 AND strm_path=?', (row['id'], row['strm_path'])).fetchone():
        raise RecoveryError('路径由其他有效分享占用')
    if db.execute('SELECT 1 FROM owned_outputs WHERE relative_path=?', (row['strm_path'],)).fetchone():
        raise RecoveryError('路径由其他受管文件占用')
    ext = source_extension(db, row)
    data = share_play_url(base, row['id'], Path(row['strm_path']).stem + ext).encode()
    digest = hashlib.sha256(data).hexdigest()
    identity = hashlib.sha256(json.dumps([row['id'], row['share_code'], row['receive_code'], row['file_id'], row['strm_path'], row['token_hash'], ext], ensure_ascii=False).encode()).hexdigest()
    intent = intent_for(db, batch, row['id'])
    if intent and (intent['relative_path'] != row['strm_path'] or intent['identity'] != identity or intent['digest'] != digest):
        raise RecoveryError('恢复意图与当前身份不一致')
    if row['revoked'] != 1 and not (row['revoked'] == 0 and intent and intent['state'] == 'done'):
        raise RecoveryError('分享记录状态已变化')
    fd, name = parent_fd(root, row['strm_path'])
    try: current = read_target(fd, name)
    finally: os.close(fd)
    if current is not None and (not intent or hashlib.sha256(current).hexdigest() != digest):
        raise RecoveryError('目标已存在且无法确认归属')
    if row['revoked'] == 0 and current is None: raise RecoveryError('已恢复登记但文件缺失')
    state = 'done' if row['revoked'] == 0 else 'resume' if current is not None else 'ready'
    return row, data, digest, identity, state


def inspect_batch(data, root, batch, count):
    db = connect(data)
    try:
        base = playback_base(db, data)
        result = []
        for item in entries(db, batch, count):
            try:
                row, _, _, _, state = verify(db, root, base, batch, item)
                result.append({'id': row['id'], 'path': row['strm_path'], 'state': state})
            except RecoveryError as exc:
                result.append({'id': item['share_id'], 'path': item['relative_path'], 'state': 'blocked', 'reason': str(exc)})
        return result
    finally: db.close()


def apply_batch(data, root, batch, count):
    if any(x['state'] == 'blocked' for x in inspect_batch(data, root, batch, count)):
        raise RecoveryError('预检未通过，未执行')
    db = connect(data, True)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS share_recovery_intents(batch TEXT NOT NULL, share_id TEXT NOT NULL, relative_path TEXT NOT NULL, identity TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(batch,share_id))')
        db.commit()
        base = playback_base(db, data)
        for item in entries(db, batch, count):
            db.execute('BEGIN IMMEDIATE')
            try:
                row, payload, digest, identity, state = verify(db, root, base, batch, item)
                if state == 'done':
                    db.commit(); continue
                db.execute('INSERT OR IGNORE INTO share_recovery_intents VALUES(?,?,?,?,?,?,?)', (batch,row['id'],row['strm_path'],identity,digest,'planned',time.time()))
                db.commit()
            except BaseException:
                db.rollback(); raise
            fd, name = parent_fd(root, row['strm_path'])
            try:
                existing = read_target(fd, name)
                if existing is None:
                    tmp_name = '.strimkeep-recover-' + secrets.token_hex(16)
                    tmp = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
                    try:
                        with os.fdopen(tmp, 'wb') as stream:
                            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                        os.link(tmp_name, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                        os.fsync(fd)
                    finally:
                        try: os.unlink(tmp_name, dir_fd=fd)
                        except FileNotFoundError: pass
                elif hashlib.sha256(existing).hexdigest() != digest:
                    raise RecoveryError('目标在恢复期间发生变化')
            finally: os.close(fd)
            db.execute('BEGIN IMMEDIATE')
            try:
                current, _, check_digest, check_identity, _ = verify(db, root, base, batch, item)
                if check_digest != digest or check_identity != identity: raise RecoveryError('恢复期间来源已变化')
                if db.execute('UPDATE share_strms SET revoked=0 WHERE id=? AND revoked=1 AND strm_path=?', (current['id'], current['strm_path'])).rowcount != 1:
                    raise RecoveryError('登记状态已变化')
                pending = db.execute("SELECT value FROM meta WHERE key='share_url_refresh_pending'").fetchone()
                try: paths = json.loads(pending['value']) if pending else []
                except (TypeError, ValueError): raise RecoveryError('Emby 待刷新清单无效') from None
                if not isinstance(paths, list) or any(not isinstance(x, str) for x in paths): raise RecoveryError('Emby 待刷新清单无效')
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('share_url_refresh_pending',?)", (json.dumps(sorted(set(paths) | {current['strm_path']}), ensure_ascii=False),))
                db.execute("UPDATE share_recovery_intents SET state='done' WHERE batch=? AND share_id=?", (batch,current['id']))
                db.commit()
            except BaseException:
                db.rollback(); raise
        return inspect_batch(data, root, batch, count)
    finally: db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', required=True)
    parser.add_argument('--expect-count', required=True, type=int)
    parser.add_argument('--data', default=os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
    parser.add_argument('--root', default=os.environ.get('STRIMKEEP_STRM_DIR', '/strm'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.batch != '1793772724.330' or args.expect_count != 27:
        parser.error('本工具仅允许恢复已核实的指定事故批次及 27 条记录')
    try:
        rows = (apply_batch if args.apply else inspect_batch)(args.data, args.root, args.batch, args.expect_count)
        for row in rows: print(row['state'], row['path'], row.get('reason', ''))
        print('items:', len(rows))
        return int(any(row['state'] == 'blocked' for row in rows))
    except (RecoveryError, sqlite3.Error, OSError) as exc:
        print('blocked:', str(exc)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
