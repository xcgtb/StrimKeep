"""Account-bound output ownership, with hashes and durable publish intents."""
import hashlib
import json
import time
from pathlib import Path

from .media import SUBTITLES
from .playaddr import candidates, play115_candidates


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Outputs:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def path(self, relative):
        from .core import UncertainState
        path = self.engine.root / relative
        if (Path(relative).is_absolute() or not path.resolve().is_relative_to(self.engine.root)
                or path.suffix.lower() not in SUBTITLES | {'.strm'}):
            raise UncertainState('本地输出路径或类型不安全，已停止。')
        return path

    def get(self, account, fid):
        return self.store.db.execute('SELECT * FROM output_registry WHERE account_id=? AND file_id=?', (account, fid)).fetchone()

    def remember(self, account, source, trail, plan):
        files = plan.get('local_files', {})
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
                (account, source['id'], encode(source), encode(trail), plan['playback_id'],
                 plan['token_cipher'], plan['relative_path'], encode(files), time.time()))
            for relative, checksum in files.items():
                self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)', (relative, account, source['id'], checksum))

    def rebuilt_digests(self, account, fid, relative):
        """本受管播放身份在本地可能出现的全部合法内容指纹。

        地址格式有可读段/无可读段两种历史形态，写盘时用的 base 也可能随配置
        调整过；只要磁盘字节等于其中任何一种，它就仍然是本项目自己的输出。
        """
        row = self.store.db.execute(
            'SELECT * FROM output_registry WHERE account_id=? AND file_id=?', (account, fid)).fetchone()
        if not row:
            return set()
        try:
            token = self.store.crypto.decrypt(row['token_cipher'].encode()).decode()
        except Exception:
            return set()
        bases = {self.store.get('playback_base') or ''}
        saved = self.store.db.execute(
            'SELECT i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=?',
            (fid,)).fetchone()
        if saved:
            try:
                previous = json.loads(saved['plan'])
                if previous.get('playback_base'):
                    bases.add(previous['playback_base'])
            except (ValueError, TypeError):
                pass
        try:
            source = json.loads(row['source'])
        except (ValueError, TypeError):
            source = {}
        digests = set()
        for base in bases:
            for form in candidates(base, row['playback_id'], Path(relative).stem, token):
                digests.add(hashlib.sha256(form).hexdigest())
            for form in play115_candidates(base, source):
                digests.add(hashlib.sha256(form).hexdigest())
        return digests

    def adopt(self, account, fid, relative, checksum):
        """把「内容与预期不符但可证明是本项目输出」的文件重新登记归属。

        只刷新指纹，不改内容、不改播放身份：这是自愈，不是覆盖。
        """
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                                  (relative, account, fid, checksum))
            row = self.store.db.execute(
                'SELECT files FROM output_registry WHERE account_id=? AND file_id=?', (account, fid)).fetchone()
            if row:
                files = json.loads(row['files'])
                files[relative] = checksum
                self.store.db.execute('UPDATE output_registry SET files=? WHERE account_id=? AND file_id=?',
                                      (encode(files), account, fid))
        self.store.event('output', '已认领历史格式输出并刷新归属指纹：' + relative)

    def guard(self, account, fid, relative, previous=None):
        from .core import UncertainState
        path = self.path(relative)
        owner = self.store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
        if not path.exists():
            # 没有实际文件可被覆盖。历史登记（洗版淘汰、手动删除、目录改名）
            # 不能永久占位，否则同名的新版本会被判成「属于另一账号」而永远写不进来。
            if owner and (owner['account_id'], owner['file_id']) != (account, fid):
                with self.store.db:
                    self.store.db.execute(
                        'DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?',
                        (relative, owner['account_id'], owner['file_id']))
                self.store.event('output', '释放已失效的归属登记（文件已不存在）：' + relative)
            return
        if owner and (owner['account_id'], owner['file_id']) != (account, fid):
            raise UncertainState('本地路径属于另一账号或文件，停止覆盖。')
        expected = owner['digest'] if owner else (previous or {}).get(relative)
        if not path.is_file() or path.stat().st_size > 20*1024*1024:
            raise UncertainState('本地文件归属不明或已被修改，已保留并停止覆盖。')
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual == expected:
            return
        # 自愈：内容等于「按当前或历史配置重建」的产物时，认领并刷新指纹。
        # 地址格式演进（可读段）会走到这里，不能被当成外部篡改永久拦住。
        if actual in self.rebuilt_digests(account, fid, relative):
            self.adopt(account, fid, relative, actual)
            return
        raise UncertainState('本地文件归属不明或已被修改，已保留并停止覆盖。')

    def mark_removed(self, job_id, relative):
        """Persist path notifications before unlink, including deletion-only sync."""
        if Path(relative).suffix.lower() != '.strm': return
        config=json.loads(self.engine.job(job_id)['config'])
        paths=set(config.get('emby_deleted_paths',[]))
        if relative not in paths:
            config['emby_deleted_paths']=sorted(paths|{relative})
            self.engine.update_job(job_id,config=encode(config))

    def unlink_recorded(self, job_id, relative, path, item_id=None, purge=False):
        """Record intent before unlink; never claim uncertain interruptions as completed."""
        tracked = Path(relative).suffix.lower() == '.strm'
        if tracked:
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)', (job_id, 'remove_pending', relative))
                if item_id is not None:
                    self.store.db.execute('INSERT OR IGNORE INTO strm_change_items VALUES(?,?,?,?)', (job_id,item_id,'remove_pending',relative))
        from .internal_delete import remember
        if tracked and not remember(self.store, [relative]):
            raise RuntimeError('内部删除登记失败，已保留本地文件。')
        if purge and tracked:
            # 联动清理同一 STRM 的专属侧车与空目录；删不掉就报错，不假装完成。
            from .fsops import purge_strm
            purge_strm(path, self.engine.root, store=self.store)
            if path.exists():
                raise OSError('本地 STRM 未能删除，已保留。')
        else:
            path.unlink()
            # STRM 没了，它自己的媒体信息 sidecar 也不能留：否则会变成孤儿，
            # 还可能被同名的新文件误当成「已有媒体信息」。
            sidecar = path.with_name(path.stem + '-media-info.json')
            try:
                if sidecar.is_file():
                    sidecar.unlink()
            except OSError:
                pass
        if tracked:
            with self.store.db:
                self.store.db.execute('DELETE FROM strm_changes WHERE job_id=? AND action=? AND path=?', (job_id, 'remove_pending', relative))
                self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)', (job_id, 'removed', relative))
                # STRM 已经不存在了，它的媒体信息队列行没有意义。
                self.store.db.execute(
                    "DELETE FROM native_media_info_queue WHERE json_extract(target,'$.relative')=?",
                    (relative,))
                if item_id is not None:
                    self.store.db.execute('DELETE FROM strm_change_items WHERE job_id=? AND item_id=? AND action=? AND path=?', (job_id,item_id,'remove_pending',relative))
                    self.store.db.execute('INSERT OR IGNORE INTO strm_change_items VALUES(?,?,?,?)', (job_id,item_id,'removed',relative))

    def retire(self, job_id, account, fid, files, keep=()):
        """Repeatable after restart. A new owner can never be unlinked by an old intent."""
        warnings = []
        for relative, checksum in files.items():
            if relative in keep: continue
            self.engine.boundary(job_id)
            path = self.path(relative)
            owner = self.store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
            if owner and (owner['account_id'], owner['file_id']) != (account, fid): continue
            if path.exists():
                if not path.is_file() or path.stat().st_size > 20*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                    warnings.append('已保留被修改的本地文件：' + relative)
                    continue
                self.engine.audit(job_id, 0, '清理已确认失效的受管输出：' + relative)
                self.mark_removed(job_id,relative)
                self.unlink_recorded(job_id,relative,path, purge=Path(relative).suffix.lower()=='.strm')
            else:
                self.mark_removed(job_id,relative)
            with self.store.db:
                self.store.db.execute('DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?', (relative, account, fid))
            config = json.loads(self.engine.job(job_id)['config'])
            if not config.get('cleanup_empty', config.get('strm_options',{}).get('cleanup_empty',True)):
                continue
            parent = path.parent
            while parent != self.engine.root and parent.is_relative_to(self.engine.root):
                try: parent.rmdir()
                except OSError: break
                parent = parent.parent
        return warnings

    async def import_managed(self, job_id, account, selected_paths):
        """Adopt old task evidence locally, scoped by selected roots; no archive walk."""
        # Read the potentially large historical join off the event loop using
        # the store's per-thread connection. Publication remains gated by the caller.
        def read_candidates():
            return self.store.db.execute('SELECT o.file_id,i.original,i.plan,j.config FROM organized_files o JOIN organize_items i ON i.id=o.item_id JOIN organize_jobs j ON j.id=o.job_id LEFT JOIN output_registry r ON r.file_id=o.file_id AND r.account_id=? WHERE r.file_id IS NULL',(account,)).fetchall()
        rows = await self.store.run_in_thread(read_candidates)
        for row in rows:
            self.engine.boundary(job_id)
            config, plan, source = (json.loads(row[k]) for k in ('config','plan','original'))
            if config.get('account_id') != account or not plan.get('token_cipher'): continue
            root = config.get('archive_cid'); trail = [root,plan.get('dest')]
            for cid,path in selected_paths.items():
                ids = [x['id'] for x in path]
                if root in ids:
                    prefix = [x['name'] for x in path[ids.index(root)+1:]]
                    if plan.get('parts',[])[:len(prefix)] == prefix: trail.append(cid)
            if not set(selected_paths).intersection(trail): continue
            mapping = self.store.db.execute('SELECT revoked FROM playbacks WHERE id=?',(plan['playback_id'],)).fetchone()
            if not mapping or mapping['revoked']: continue
            files = self.engine.upgrader.owned_files(row)
            if not files: continue
            collision = False
            for relative in files:
                owner = self.store.db.execute('SELECT account_id,file_id FROM owned_outputs WHERE relative_path=?',(relative,)).fetchone()
                if owner and (owner['account_id'],owner['file_id']) != (account,source['id']): collision=True
            if collision: continue
            source.update(name=plan['target_name'],parent_id=plan['dest'])
            self.remember(account,source,list(dict.fromkeys(trail)),{**plan,'local_files':files})
