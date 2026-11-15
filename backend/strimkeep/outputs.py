"""本地输出的「查询入口」——数据源就是磁盘，不再有任何登记表。

旧版本这里是一张台账（output_registry / owned_outputs）：写过什么、内容指纹、
播放令牌都往里记，同步时再拿它当「已经同步过」的依据。结果是本地删掉文件后
同步仍然跳过。现在同一份信息全部可以从磁盘推导：

- 路径：STRM 内容里的 /play115/<pickcode>/<fid>/<sha1>/<size>/<name>；
- 指纹：直接算文件的 sha256；
- 归属：身份里的 fid 就是网盘文件。

所以这个类只剩「路径解析 + 按 fid 找本地文件」，写操作是空实现（保留调用点，
真正落盘在 sync / organizer 里完成）。
"""
import hashlib
import json
import time
from pathlib import Path

from .identity import hint, parse_file
from .media import SUBTITLES


class Outputs:
    # 扫盘结果缓存：同一轮同步里会被逐文件查询，不能每次都走一遍目录树。
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
        now = time.time()
        if self._cache is not None and now - self._cache_at < self.CACHE_SECONDS:
            return self._cache
        index = {}
        root = self.engine.root
        for path in root.rglob('*.strm'):
            try:
                raw = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                continue
            identity = parse_file(path) or hint(raw)
            if not identity or identity.get('kind') != 'netdisk':
                continue
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                continue
            index[str(identity['file_id'])] = {
                'relative_path': relative,
                'identity': identity,
                'path': path,
            }
        self._cache, self._cache_at = index, now
        return index

    def digest(self, path):
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None

    def get(self, account, fid):
        """按文件身份取本地输出；没有就是没有（不再有「我记过」这回事）。"""
        entry = self.scan().get(str(fid))
        if not entry:
            return None
        path = entry['path']
        checksum = self.digest(path)
        source = {**entry['identity'], 'id': str(fid)}
        return {
            'account_id': account,
            'file_id': str(fid),
            'source': json.dumps(source, ensure_ascii=False),
            'trail': json.dumps([]),
            'playback_id': None,
            'token_cipher': '',
            'relative_path': entry['relative_path'],
            'files': json.dumps({entry['relative_path']: checksum}, ensure_ascii=False),
            'active': 1,
            'updated': path.stat().st_mtime if path.exists() else 0,
        }

    # ---- 索引重建：两张旧表降级为磁盘缓存 ------------------------------
    def reindex(self, account):
        """按磁盘现状重写 output_registry / owned_outputs。

        它们不再是「我处理过什么」的记录，只是给旧查询用的索引；
        本地文件被删掉后，重建一次索引里也就没有了，不会再挡住同步。
        """
        account = str(account)
        now = time.time()
        rows = []
        for fid, entry in self.scan().items():
            relative = entry['relative_path']
            checksum = self.digest(entry['path'])
            source = {**entry['identity'], 'id': str(fid)}
            rows.append((account, str(fid), json.dumps(source, ensure_ascii=False, separators=(',', ':')),
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
