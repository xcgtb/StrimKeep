"""本地输出的查询入口：事实在磁盘和云端清单，两张表只是索引缓存。

旧版本这里是一张台账：写过什么、内容指纹、播放令牌都往里记，同步时再拿它当
「已经同步过」的依据，于是本地删掉文件后同步仍然跳过。现在：

- 同步判定只看「云端清单 + 本地文件」；
- 两张表由每轮同步结束时的 `reindex()` 按磁盘现状重写，只用于查询加速；
- `get()` 走索引表（一次主键查询），不再为了一个文件号扫整棵树。
"""
import hashlib
import json
import time
from pathlib import Path

from .identity import hint, parse_file
from .media import SUBTITLES


class Outputs:
    CACHE_SECONDS = 5.0

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self._cache = None
        self._cache_at = 0.0

    def path(self, relative):
        from .core import UncertainState
        path = self.engine.root / relative
        if (Path(relative).is_absolute() or not path.resolve().is_relative_to(self.engine.root)
                or path.suffix.lower() not in SUBTITLES | {'.strm'}):
            raise UncertainState('本地输出路径或类型不安全，已停止。')
        return path

    # ---- 扫盘索引 -------------------------------------------------------
    def invalidate(self):
        self._cache, self._cache_at = None, 0.0

    def scan(self):
        """整棵树扫一遍（只给核对类流程用，调用方应放进线程）。"""
        now = time.time()
        if self._cache is not None and now - self._cache_at < self.CACHE_SECONDS:
            return self._cache
        index = {}
        root = self.engine.root
        for path in root.rglob('*.strm'):
            identity = parse_file(path)
            if not identity:
                try:
                    identity = hint(path.read_text(encoding='utf-8'))
                except (OSError, UnicodeError):
                    identity = None
            if not identity or identity.get('kind') != 'netdisk':
                continue
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                continue
            index[str(identity['file_id'])] = {
                'relative_path': relative, 'identity': identity, 'path': path}
        self._cache, self._cache_at = index, now
        return index

    def digest(self, path):
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None

    def get(self, account, fid):
        """按文件身份取本地输出：一次索引查询，不再为了一个文件号扫整棵树。"""
        row = self.store.db.execute(
            'SELECT account_id,file_id,source,trail,playback_id,token_cipher,relative_path,'
            'files,active,updated FROM output_registry WHERE account_id=? AND file_id=?',
            (str(account), str(fid))).fetchone()
        if row and row['active']:
            return dict(row)
        # 索引还没建过（例如刚升级）：用整理登记的路径定点确认，只解析这一个文件。
        for candidate in self.store.db.execute(
                'SELECT relative_path FROM organized_files WHERE file_id=?', (str(fid),)):
            try:
                path = self.engine.root / candidate['relative_path']
            except (KeyError, TypeError):
                continue
            identity = parse_file(path)
            if not identity:
                try:
                    identity = hint(path.read_text(encoding='utf-8'))
                except (OSError, UnicodeError):
                    identity = None
            if not identity or str(identity.get('file_id')) != str(fid):
                continue
            checksum = self.digest(path)
            source = {**identity, 'id': str(fid)}
            return {'account_id': account, 'file_id': str(fid),
                    'source': json.dumps(source, ensure_ascii=False),
                    'trail': json.dumps([]), 'playback_id': None, 'token_cipher': '',
                    'relative_path': candidate['relative_path'],
                    'files': json.dumps({candidate['relative_path']: checksum}, ensure_ascii=False),
                    'active': 1, 'updated': path.stat().st_mtime if path.exists() else 0}
        return None

    def note(self, account, relative, path, identity):
        """刚落盘的一份输出只更新它自己那一行（O(1)），不扫盘。"""
        if not identity:
            return
        fid = str(identity.get('file_id') or '')
        if not fid:
            return
        checksum = self.digest(path)
        source = {**identity, 'id': fid}
        stamp = time.time()
        with self.store.db:
            self.store.db.execute(
                'INSERT OR REPLACE INTO output_registry(account_id,file_id,source,trail,playback_id,'
                'token_cipher,relative_path,files,active,updated) VALUES(?,?,?,?,?,?,?,?,1,?)',
                (str(account), fid, json.dumps(source, ensure_ascii=False, separators=(',', ':')),
                 '[]', '', '', relative,
                 json.dumps({relative: checksum}, ensure_ascii=False, separators=(',', ':')), stamp))
            self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                                  (relative, str(account), fid, checksum))

    # ---- 索引重建：两张旧表降级为磁盘缓存 ------------------------------
    def reindex(self, account):
        """按磁盘现状重写 output_registry / owned_outputs（调用方应放进线程）。"""
        account = str(account)
        now = time.time()
        rows = []
        for fid, entry in self.scan().items():
            relative = entry['relative_path']
            checksum = self.digest(entry['path'])
            source = {**entry['identity'], 'id': str(fid)}
            rows.append((account, str(fid),
                         json.dumps(source, ensure_ascii=False, separators=(',', ':')),
                         json.dumps([], separators=(',', ':')), relative,
                         json.dumps({relative: checksum}, ensure_ascii=False, separators=(',', ':')), now))
        with self.store.db:
            self.store.db.execute('DELETE FROM output_registry WHERE account_id=?', (account,))
            self.store.db.execute('DELETE FROM owned_outputs WHERE account_id=?', (account,))
            for row in rows:
                self.store.db.execute(
                    'INSERT OR REPLACE INTO output_registry(account_id,file_id,source,trail,playback_id,'
                    'token_cipher,relative_path,files,active,updated) VALUES(?,?,?,?,?,?,?,?,1,?)',
                    (row[0], row[1], row[2], row[3], '', '', row[4], row[5], row[6]))
                self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                                      (row[4], row[0], row[1], json.loads(row[5])[row[4]]))
        self.invalidate()

    # ---- 写操作：不再登记，保留接口 ------------------------------------
    def remember(self, account, source, trail, plan):
        self.invalidate()

    def guard(self, account, fid, relative, previous=None):
        return None

    def mark_removed(self, job_id, relative):
        self.invalidate()

    def unlink_recorded(self, job_id, relative, path, item_id=None, purge=False):
        """删本地文件（唯一真正执行的写操作）。"""
        self.invalidate()
        target = Path(path)
        if not target.exists():
            return []
        try:
            from .internal_delete import remember
            if not remember(self.store, [relative]):
                return ['内部删除登记失败，已保留：' + str(relative)]
            target.unlink()
            sidecar = target.with_name(target.stem + '-media-info.json')
            if sidecar.is_file():
                try:
                    sidecar.unlink()
                except OSError:
                    pass
            if job_id is not None:
                with self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)',
                                          (job_id, 'removed', relative))
        except Exception as exc:                     # pragma: no cover - 保守失败
            return ['删除失败：' + str(exc)[:60]]
        return []

    def retire(self, job_id, account, fid, files, keep=()):
        return []

    def adopt(self, account, fid, relative, checksum):
        self.invalidate()

    def rebuilt_digests(self, account, fid, relative):
        return set()

    async def import_managed(self, job_id, account, selected_paths):
        self.invalidate()
