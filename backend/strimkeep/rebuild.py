"""本地 STRM 全量重建：预览、执行、快照回滚（0.8.20）。

为什么要单独一个模块：重建是唯一「一次性动很多本地文件」的入口，原来实现在
`create_app` 里同步遍历，等于把 892 次读文件 + 写文件 + 2 次 UPDATE 全部压在
事件循环上（审计 P0-1 的一部分）。这里把文件 I/O 放进 `asyncio.to_thread`，
DB 写入留在主线程、按一次事务提交，并且：

- 只动本地输出与台账，云端一个字节都不碰；
- 执行前把将被覆盖的旧内容快照进 `strm_rebuild_backups`（每条 STRM 只有一行 URL，
  892 条约 180 KB），支持一键回滚；
- `dry_run=True` 只读不写，先给人看清楚「会改哪些、缺哪些」。

入口：`POST /api/v1/strm/rebuild[?dry_run=1]`、`GET /api/v1/strm/rebuild/status`、
`POST /api/v1/strm/rebuild/rollback`。
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path

from .core import UncertainState
from .playaddr import identity_of, play115_url, play_url

KEEP_BACKUPS = 5


class Rebuild:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        with self.store.db:
            self.store.db.execute(
                'CREATE TABLE IF NOT EXISTS strm_rebuild_backups('
                'id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,'
                'rows INTEGER NOT NULL DEFAULT 0,files TEXT NOT NULL)')

    # ---------------------------------------------------------------- 状态
    def status(self):
        return self.store.meta_json('strm_rebuild_status', {'state': 'idle'})

    def set_status(self, **values):
        current = self.status() or {}
        current.update(values)
        current['at'] = time.time()
        self.store.set_meta('strm_rebuild_status', current)
        return current

    def scope(self):
        account = self.engine.tasks.account()
        base = self.store.get('playback_base')
        if not base:
            raise RuntimeError('请先设置播放基础地址。')
        return account, str(base)

    # ---------------------------------------------------------------- 计划
    def items(self, account, base):
        """把台账读成待处理项：相对路径、目标字节、新 checksum。

        不含文件读写，892 行只需几毫秒，可以安全地在事件循环里调用。
        路径与后缀在这里就用 `outputs.path()` 校验一次，越界或异常的行直接跳过
        （它们本来也不该被这次重建碰）。
        """
        result = []
        rows = self.store.db.execute(
            'SELECT * FROM output_registry WHERE account_id=? ORDER BY relative_path',
            (account,)).fetchall()
        for row in rows:
            relative = row['relative_path']
            try:
                path = self.engine.outputs.path(relative)
            except UncertainState:
                continue
            try:
                token = self.store.crypto.decrypt(row['token_cipher'].encode()).decode()
            except Exception:
                token = ''
            try:
                source = json.loads(row['source'] or '{}')
            except (TypeError, ValueError):
                source = {}
            name = source.get('name') or Path(relative).stem
            # 目标地址只算一次：身份齐全用 play115，否则退回带 token 的 /play。
            ident = identity_of(source)
            url = play115_url(base, *ident, name) if ident else play_url(base, row['playback_id'], name, token)
            # 结尾换行有两种历史写法：publication/sync 写的是「无换行」，write_strm() 写的是
            # 「带换行」。0.8.20 现场实测：949 条台账里 879 条是无换行形态。重建必须保持
            # 既有风格，否则会为了一个字节把整棵树重写一遍（还会打乱 owned_outputs 指纹）。
            result.append({'relative': relative, 'path': path,
                           'content': url.encode(), 'content_nl': (url + '\n').encode()})
        return result

    # ---------------------------------------------------------------- 线程内 I/O
    @staticmethod
    def _inspect(item, write):
        """线程内执行：读盘、判断动作、按需写入。

        返回 action ∈ rewrite / unchanged / missing / failed，并带回旧内容（供快照）
        与实际写入的 checksum。

        换行策略：文件既是 `url` 又是 `url + '\\n'` 都算最新（两种历史写法都接受）；
        需要改写时沿用文件现有的结尾风格——现网 879 条是无换行形态，不能因为
        重建就整棵树重写。
        """
        path = item['path']
        outcome = {'relative': item['relative'], 'checksum': None,
                   'action': 'failed', 'before': ''}
        try:
            if not path.is_file():
                outcome['action'] = 'missing'
                return outcome
            before = path.read_bytes()
            if before in (item['content'], item['content_nl']):
                outcome['action'] = 'unchanged'
                outcome['checksum'] = hashlib.sha256(before).hexdigest()
                return outcome
            outcome['before'] = before.decode('utf-8', 'replace')
            content = item['content_nl'] if before.endswith(b'\n') else item['content']
            checksum = hashlib.sha256(content).hexdigest()
            if write:
                temporary = path.with_name(path.name + '.tmp-' + checksum[:8])
                temporary.write_bytes(content)
                temporary.replace(path)
            outcome['action'] = 'rewrite'
            outcome['checksum'] = checksum
        except OSError:
            outcome['action'] = 'failed'
        return outcome

    def _inspect_all(self, items, write):
        return [self._inspect(item, write) for item in items]

    def _restore_all(self, files):
        """线程内执行：把快照里的字节写回磁盘，返回成功条数。"""
        restored = 0
        for relative, text in files.items():
            try:
                path = self.engine.outputs.path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + '.tmp-restore')
                temporary.write_text(text, encoding='utf-8')
                temporary.replace(path)
                restored += 1
            except (UncertainState, OSError):
                continue
        return restored

    # ---------------------------------------------------------------- 快照
    def snapshot(self, contents):
        """把「将被覆盖的旧内容」写成一条快照，并只保留最近 KEEP_BACKUPS 份。"""
        with self.store.db:
            cursor = self.store.db.execute(
                'INSERT INTO strm_rebuild_backups(at,rows,files) VALUES(?,?,?)',
                (time.time(), len(contents), json.dumps(contents, ensure_ascii=False)))
            backup = cursor.lastrowid
            self.store.db.execute(
                'DELETE FROM strm_rebuild_backups WHERE id NOT IN '
                '(SELECT id FROM strm_rebuild_backups ORDER BY id DESC LIMIT ?)', (KEEP_BACKUPS,))
        return backup

    def latest_backup(self):
        row = self.store.db.execute(
            'SELECT * FROM strm_rebuild_backups ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            raise LookupError('没有可回滚的重建快照。')
        return row

    # ---------------------------------------------------------------- 执行
    async def run(self, dry_run=False):
        """dry_run=True 只预览；否则先快照、再写盘、最后更新台账。

        返回结构兼容 0.8.18（scanned/rewritten/unchanged/missing/failed），
        新增 dry_run / snapshot / elapsed_seconds 三个字段。
        """
        account, base = self.scope()
        items = self.items(account, base)
        started = time.time()
        self.set_status(state='running', dry_run=bool(dry_run), total=len(items), done=0,
                        rewritten=0, unchanged=0, missing=0, failed=0, started=started)
        inspected = await asyncio.to_thread(self._inspect_all, items, not dry_run)
        counts = {'rewritten': 0, 'unchanged': 0, 'missing': 0, 'failed': 0}
        label = {'rewrite': 'rewritten', 'unchanged': 'unchanged',
                 'missing': 'missing', 'failed': 'failed'}
        for row in inspected:
            counts[label[row['action']]] += 1
        changed = {row['relative']: row['checksum'] for row in inspected if row['action'] == 'rewrite'}
        backup = None
        if changed and not dry_run:
            backup = self.snapshot({row['relative']: row['before']
                                    for row in inspected if row['action'] == 'rewrite'})
            self.apply(changed, account)
        summary = {'scanned': len(items), 'dry_run': bool(dry_run), 'snapshot': backup,
                   'elapsed_seconds': round(time.time() - started, 2), **counts}
        self.set_status(state='done', done=len(items), **counts)
        return summary

    def apply(self, changed, account):
        """把重写结果写回台账：output_registry.files 与 owned_outputs.digest。

        单次事务：宁可整批成功或整批回滚，也不要留下「文件已改、台账没改」的中间态。
        """
        rows = self.store.db.execute(
            'SELECT account_id,file_id,files,relative_path FROM output_registry WHERE account_id=?',
            (account,)).fetchall()
        by_path = {row['relative_path']: row for row in rows}
        with self.store.db:
            for relative, checksum in changed.items():
                row = by_path.get(relative)
                if not row:
                    continue
                try:
                    files = json.loads(row['files'] or '{}')
                except (TypeError, ValueError):
                    files = {}
                files[relative] = checksum
                self.store.db.execute(
                    'UPDATE output_registry SET files=?,updated=? WHERE account_id=? AND file_id=?',
                    (json.dumps(files, ensure_ascii=False), time.time(),
                     row['account_id'], row['file_id']))
                self.store.db.execute(
                    'UPDATE owned_outputs SET digest=? WHERE relative_path=? AND account_id=? AND file_id=?',
                    (checksum, relative, row['account_id'], row['file_id']))

    async def rollback(self, backup_id=None):
        """把最近一次（或指定）快照的字节写回，并同步台账指纹。

        回滚同样只动本地文件：不动云端、不动播放身份、不通知 Emby
        （地址没变，只是内容回到旧格式）。
        """
        if backup_id:
            row = self.store.db.execute(
                'SELECT * FROM strm_rebuild_backups WHERE id=?', (backup_id,)).fetchone()
            if not row:
                raise LookupError('指定的重建快照不存在。')
        else:
            row = self.latest_backup()
        files = json.loads(row['files'] or '{}')
        restored = await asyncio.to_thread(self._restore_all, files)
        changed = {relative: hashlib.sha256(text.encode()).hexdigest()
                   for relative, text in files.items()}
        self.apply(changed, self.engine.tasks.account())
        self.set_status(state='rolled_back', done=restored, snapshot=row['id'])
        return {'restored': restored, 'snapshot': row['id'], 'at': row['at'], 'total': len(files)}

