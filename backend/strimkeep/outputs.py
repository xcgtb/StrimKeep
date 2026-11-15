"""输出索引与来源证据。

同步是否复用必须核对实时清单和本地文件；来源、令牌和附件指纹不能从
STRM 完整推导，单独保存供洗版、治理及兼容旧播放链接时核验。
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
        old = self.store.db.execute(
            'SELECT source,trail,playback_id,token_cipher,files FROM output_registry WHERE account_id=? AND file_id=?',
            (str(account), fid)).fetchone()
        source = {}
        if old:
            try:
                previous = json.loads(old['source'] or '{}')
                # Keep provenance only for the same content identity.
                if (str(previous.get('sha1') or '').upper() == str(identity.get('sha1') or '').upper()
                        and previous.get('size') == identity.get('size')):
                    source = previous
            except (TypeError, ValueError, AttributeError):
                pass
        source = {**source, **identity, 'id': fid}
        files = {}
        if old:
            try:
                for old_path, old_digest in json.loads(old['files'] or '{}').items():
                    if old_path != relative and Path(old_path).suffix.lower() in SUBTITLES:
                        target = self.path(old_path)
                        if target.is_file() and not target.is_symlink() and self.digest(target) == old_digest:
                            files[old_path] = old_digest
            except (TypeError, ValueError, AttributeError):
                pass
        files[relative] = checksum
        stamp = time.time()
        with self.store.db:
            self.store.db.execute(
                'INSERT OR REPLACE INTO output_registry(account_id,file_id,source,trail,playback_id,'
                'token_cipher,relative_path,files,active,updated) VALUES(?,?,?,?,?,?,?,?,1,?)',
                (str(account), fid, json.dumps(source, ensure_ascii=False, separators=(',', ':')),
                 old['trail'] if old else '[]', old['playback_id'] if old else '',
                 old['token_cipher'] if old else '', relative,
                 json.dumps(files, ensure_ascii=False, separators=(',', ':')), stamp))
            self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                                  (relative, str(account), fid, checksum))

    # ---- 缓存失效 -------------------------------------------------------
    def reindex(self, account):
        """Invalidate the disk cache; publication updates evidence incrementally.

        An STRM does not encode its account, source parent, subtitle ownership, or
        legacy playback secret. Reconstructing those fields from a disk scan loses
        evidence and can assign another account's files to the current account.
        """
        self.invalidate()

    # ---- 发布和归属检查 -------------------------------------------------
    def remember(self, account, source, trail, plan):
        """Persist provenance after publication, never as proof a file still exists."""
        fid = str(source['id'])
        files = plan.get('local_files') or {}
        with self.store.db:
            self.store.db.execute(
                'INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
                (str(account), fid, json.dumps(source, ensure_ascii=False, separators=(',', ':')),
                 json.dumps(trail, ensure_ascii=False, separators=(',', ':')),
                 plan.get('playback_id') or '', plan.get('token_cipher') or '',
                 plan['relative_path'], json.dumps(files, ensure_ascii=False, separators=(',', ':')),
                 time.time()))
            for relative, checksum in files.items():
                self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                                      (relative, str(account), fid, checksum))
        self.invalidate()

    def guard(self, account, fid, relative, previous=None):
        from .core import UncertainState
        target = self.path(relative)
        if not target.exists() and not target.is_symlink():
            return
        if target.is_symlink() or not target.is_file():
            raise UncertainState('本地同名路径不是普通文件，已保留。')
        owner = self.store.db.execute(
            'SELECT account_id,file_id FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
        if owner and (str(owner['account_id']), str(owner['file_id'])) != (str(account), str(fid)):
            raise UncertainState('本地路径属于另一账号或文件，已保留。')
        checksum = self.digest(target)
        if checksum and checksum == (previous or {}).get(relative):
            return
        identity = parse_file(target)
        if identity and identity.get('kind') == 'netdisk' and str(identity.get('file_id')) == str(fid):
            return
        if identity and identity.get('kind') == 'playback':
            mapping = self.store.db.execute('SELECT file_id,revoked FROM playbacks WHERE id=?',
                                            (identity.get('playback_id'),)).fetchone()
            if mapping and str(mapping['file_id']) == str(fid) and not mapping['revoked']:
                return
        raise UncertainState('本地已有同名的其他文件或无法核实的文件，已保留。')

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
