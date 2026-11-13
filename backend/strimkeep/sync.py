"""Read-only cloud synchronization; cleanup requires a complete, rechecked manifest."""
import hashlib
import json
import secrets
import time
from pathlib import Path

import httpx
from .cloud115 import CloudError, FATAL_CODES
from .media import SUBTITLES, component, bind_subtitles, subtitle_suffix
from .strm_options import StrmOptions, skip_reason, write_new
from .organize_options import library_root
from .playaddr import candidates, identity_of, play115_candidates, play115_url, play_url
from .store import digest
from .sync_hash_worker import HashWorker, HashWorkerError


class Sync:
    FULL_SCAN_SECONDS = 1800
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.hash_worker = HashWorker()
        # 播放 Token 按密文缓存：同一批输出大多共用同一个空口令，逐个 Fernet 解密很贵。
        self._tokens = {}
        # 失败留痕的节流表：同一类失败 5 分钟内只写一条事件，别让巡检灌满事件表。
        self._notices = {}

    NOTICE_SECONDS = 300
    NOTICE_KEYS_MAX = 200

    def notice(self, key, message):
        """同一类失败在 NOTICE_SECONDS 内只留一条事件。

        下面那两处 except 都是「尽力而为」的分支（附属文件清理、清单解析）：
        失败不影响台账正确性，但静默跳过等于放弃可观测性——真出事时查不到痕迹。
        节流留痕兼顾两者：有痕迹，又不会每轮巡检都灌一条。
        """
        now = time.time()
        if now - self._notices.get(key, 0.0) < self.NOTICE_SECONDS:
            return
        if len(self._notices) >= self.NOTICE_KEYS_MAX:
            self._notices.clear()
        self._notices[key] = now
        try:
            self.store.event('sync', message)
        except Exception:
            # 留痕自身失败不能反过来影响巡检主流程。
            pass

    @property
    def cloud(self): return self.engine.cloud

    def invalidate(self, account):
        # Invalidate before a possible remote mutation, including lost responses.
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                                  ('sync_revision:'+account, secrets.token_hex(16)))

    def scope(self, config):
        return hashlib.sha256(json.dumps([sorted(config['source_cids']),config.get('archive_cid'), {k:config.get(k) for k in StrmOptions.model_fields}],sort_keys=True).encode()).hexdigest()

    async def unchanged(self, source, config):
        """A read-only fast path. It never publishes, removes, or reauthorizes files."""
        account = self.engine.index.account(config)
        if account+':'+source['id'] in json.loads(self.store.meta('library_name_pending') or '{}'):return None
        old = self.engine.outputs.get(account, source['id'])
        if not old or not old['active']: return None
        previous = json.loads(old['source'])
        fingerprint = self.engine.measurements.fingerprint
        if not fingerprint(source) or fingerprint(previous) != fingerprint(source): return None
        if any(previous.get(k) != source.get(k) for k in ('id','name','parent_id','size')): return None
        trail = [x['id'] for x in source['_path']]
        recorded_trail = json.loads(old['trail'])
        # Organizer historically records only archive root + immediate parent.
        # A fresh full scan can repair this abbreviated trail only after the
        # existing identity, output path, ownership and bytes are all checked.
        now = time.time()
        scan_at = source.get('_scan_at')
        archive = str(config.get('archive_cid') or '')
        abbreviated = (recorded_trail != trail and len(recorded_trail) == 2
                       and recorded_trail == [archive, source.get('parent_id')]
                       and archive in trail and trail[-1] == source.get('parent_id')
                       and len(trail) == len(set(trail))
                       and isinstance(scan_at, (int, float)) and 0 <= now - scan_at < 900)
        if recorded_trail != trail and not abbreviated: return None
        if source.get('subtitle_warning'): return None
        def subtitle_keys(items):
            result = []
            for sub in items:
                key = fingerprint(sub)
                if not key: return None
                result.append((sub['id'], sub['name'], sub['parent_id'], key))
            return sorted(result)
        keys = subtitle_keys(source.get('subtitles', []))
        if keys is None or keys != subtitle_keys(previous.get('subtitles', [])): return None
        relative = self.relative(source, config)
        if relative != old['relative_path']: return None
        mapping = self.store.db.execute('SELECT * FROM playbacks WHERE id=?',(old['playback_id'],)).fetchone()
        token = self.store.crypto.decrypt(old['token_cipher'].encode()).decode()
        if not mapping or mapping['revoked'] or mapping['file_id'] != source['id'] : return None
        files = json.loads(old['files'])
        # 身份齐全时只认 play115 形态：存量 /play/<token> 地址会在本轮改写成
        # /play115/...（0.7.12 起的规范形态）。身份不全（缺 sha1/pickcode）的条目
        # 只能写令牌地址，继续认旧形态，否则每次同步都会反复重写同一份。
        forms = []
        if identity_of(previous) is None:
            forms += candidates(config['playback_base'], old['playback_id'],
                                source['name'], token)
            # 老版本使用无扩展名的 STRM 主干，仍接受其已登记的播放地址。
            forms += candidates(config['playback_base'], old['playback_id'],
                                Path(old['relative_path']).stem, token)
        # 同一份 STRM 当「变了」重写一遍，还会连带触发 Emby 通知。
        forms += play115_candidates(config['playback_base'], previous, source['name'])
        forms += play115_candidates(config['playback_base'], previous,
                                    Path(old['relative_path']).stem)
        if files.get(relative) not in {hashlib.sha256(content).hexdigest() for content in forms}:
            return None
        for name, checksum in files.items():
            local = self.engine.outputs.path(name)
            owner = self.store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?',(name,)).fetchone()
            if not owner or (owner['account_id'],owner['file_id'],owner['digest']) != (account,source['id'],checksum): return None
            try:
                if await self.hash_worker.checksum(local) != checksum: return None
            except HashWorkerError as exc:
                from .core import UncertainState
                raise UncertainState(str(exc)) from None
        result = {'relative_path':relative, 'target_name':source['name'], 'playback_id':old['playback_id'],
                  'token_cipher':old['token_cipher'], 'local_files':files, 'warnings':[], 'sync_unchanged':True}
        if abbreviated:
            result['_sync_trail_repair'] = {'old': old['trail'], 'new': trail}
        return result

    async def cached_manifest(self, jid, config):
        from .core import dumps
        # 手动同步必须重新发现云端新增文件；仅定时增量可复用近期清单。
        if not config.get('scheduled') or config.get('full_scan', True) or set(config.get('metadata_extensions', [])) - SUBTITLES:
            self.store.event('sync', '缓存未命中：手动任务或强制全量，直接重扫。')
            return False
        account = self.engine.index.account(config)
        snapshot = self.store.db.execute('SELECT * FROM sync_snapshots WHERE account_id=? AND scope=?',(account,self.scope(config))).fetchone()
        if not snapshot:
            self.store.event('sync', '缓存未命中：无快照（首次或范围变化）。'); return False
        if not 0 <= time.time()-snapshot['checked'] < self.FULL_SCAN_SECONDS:
            self.store.event('sync', '缓存未命中：快照已过期（%.0f 分钟）。' % ((time.time()-snapshot['checked'])/60)); return False
        if snapshot['revision'] != self.store.meta('sync_revision:'+account):
            self.store.event('sync', '缓存未命中：云端版本号已变（整理期间有变更）。'); return False
        paths = {cid: await self.cloud.path_info(cid) for cid in config['source_cids']}
        if paths != json.loads(snapshot['paths']):
            self.store.event('sync', '缓存未命中：来源目录路径已变化。'); return False
        # A missing/edited output, address or subtitle change forces fresh discovery
        # before any repair; stale cached paths never authorize publication/deletion.
        rows = self.store.db.execute('SELECT original FROM organize_items WHERE job_id=?',(snapshot['job_id'],)).fetchall()
        async with self.engine.publication_gate:
            for row in rows:
                self.engine.boundary(jid)
                verdict = await self.unchanged(json.loads(row['original']), config)
                if not verdict:
                    self.store.event('sync', '缓存未命中：清单内文件指纹变化（%s）。'
                                     % (json.loads(row['original']).get('name') or '')[:60])
                    return False
        self.store.jobs.copy_manifest(snapshot['job_id'], jid)
        current = json.loads(self.engine.job(jid)['config']);current['sync_mode']='cached';current['manifest_at']=time.time()
        self.engine.update_job(jid, config=dumps(current))
        return True

    async def download_manifest(self, jid, config, effective, paths):
        """递归目录、文件和批量资料组装成完整路径，不逐目录请求。"""
        from .core import UncertainState
        files, stats, all_dirs = {}, {}, set()
        for order, root in enumerate(effective, 1):
            self.engine.boundary(jid)
            synthetic={'_path':paths[root], '_root':root, 'name':'scope.strm'}
            detail={'id':root,'name':paths[root][-1]['name'],
                    'local_path':str(self.engine.root / Path(self.relative(synthetic,config)).parent),
                    'directories':0,'files':0,'scan_seconds':0.0}
            await self.engine.notifications.publish(f'strm:{jid}:scan-start:{root}','▶️ [%d/%d] 开始同步目录ID: %s' % (order, len(effective), root),
                f'进度：{order}/{len(effective)}\n目录 ID：{root}\n云盘目录：{detail["name"]}',
                job_id=jid,category='strm_summary')
            started=time.monotonic()
            dirs, entries=await self.cloud.download_tree(root)
            nodes={}
            for d in dirs:
                if d['id'] == root or d['id'] in nodes: raise UncertainState('目录身份重复。')
                nodes[d['id']]=d
            trails={root:paths[root]}
            for cid in nodes:
                chain=[];visiting=set();current=cid
                while current not in trails:
                    if current in visiting or current not in nodes:
                        raise UncertainState('递归目录存在循环或缺失上级。')
                    visiting.add(current);node=nodes[current];chain.append(node);current=node['parent_id']
                trail=trails[current]
                for node in reversed(chain):
                    trail=trail+[{'id':node['id'],'name':node['name']}]
                    trails[node['id']]=trail
            for i,entry in enumerate(entries):
                if i % 256 == 0:self.engine.boundary(jid)
                if entry['id'] in files or entry['parent_id'] not in trails:
                    raise UncertainState('递归文件身份重复或目录缺失。')
                files[entry['id']]={**entry,'_path':trails[entry['parent_id']], '_root':root,'_scan_at':time.time()}
            all_dirs.update(trails)
            detail.update(directories=len(trails),files=len(entries),scan_seconds=time.monotonic()-started)
            stats[root]=detail
            self.engine.update_job(jid,stage=f'同步扫描：{len(all_dirs)} 个目录，{len(files)} 个文件')
        return all_dirs,files,stats

    async def recursive_manifest(self, jid, config, effective, paths):
        """Build a complete file snapshot and verify every containing directory path."""
        from .core import UncertainState
        files, stats, seen = {}, {}, set()
        for order, root in enumerate(effective, 1):
            self.engine.boundary(jid)
            synthetic = {'_path': paths[root], '_root': root, 'name': 'scope.strm'}
            detail = {'id': root, 'name': paths[root][-1]['name'],
                      'local_path': str(self.engine.root / Path(self.relative(synthetic, config)).parent),
                      'directories': 0, 'directories_exact': False, 'files': 0, 'scan_seconds': 0.0}
            await self.engine.notifications.publish(f'strm:{jid}:scan-start:{root}', '▶️ [%d/%d] 开始同步目录ID: %s' % (order, len(effective), root),
                f'进度：{order}/{len(effective)}\n目录 ID：{root}\n云盘目录：{detail["name"]}',
                job_id=jid, category='strm_summary')
            started = time.monotonic()
            entries = await self.cloud.recursive_files_verified(root)
            parent_paths = {root: paths[root]}
            for parent in sorted({entry['parent_id'] for entry in entries}):
                self.engine.boundary(jid)
                if parent not in parent_paths:
                    trail = await self.cloud.path_info(parent)
                    ids = [node['id'] for node in trail]
                    if not ids or ids[-1] != parent or len(ids) != len(set(ids)) or root not in ids:
                        raise UncertainState('递归扫描目录路径不属于所选范围。')
                    if trail[:ids.index(root)+1] != paths[root]:
                        raise UncertainState('递归扫描期间根目录路径发生变化。')
                    parent_paths[parent] = trail
            dirs = set()
            for entry in entries:
                self.engine.boundary(jid)
                fid = entry['id']
                if fid in files:
                    raise UncertainState('递归扫描出现重复文件 ID。')
                trail = parent_paths[entry['parent_id']]
                ids = [node['id'] for node in trail]
                dirs.update(ids[ids.index(root):])
                files[fid] = {**entry, '_path': trail, '_root': root, '_scan_at': time.time()}
            dirs.add(root)
            seen.update(dirs)
            detail.update(directories=len(dirs), files=len(entries), scan_seconds=time.monotonic()-started)
            stats[root] = detail
            await self.engine.notifications.publish(
                'strm:%d:dir-done:%s' % (jid, root), '✅ 目录ID %s' % root,
                '文件：%d 个，耗时 %.2f 秒。' % (detail['files'], detail['scan_seconds']),
                job_id=jid, category='strm_summary')
            self.engine.update_job(jid, stage=f'同步扫描：核实 {len(seen)} 个文件路径目录，{len(files)} 个文件')
        return seen, files, stats

    async def manifest(self, jid, config):
        from .core import dumps, UncertainState
        roots = config['source_cids']
        revision_at_start = self.store.meta('sync_revision:'+self.engine.index.account(config))
        manifest_started_at = time.time()
        paths = {cid: await self.cloud.path_info(cid) for cid in roots}
        # Pick the outermost selected root, independent of selection order.
        effective = [cid for cid in roots if not any(other != cid and other in [x['id'] for x in paths[cid]] for other in roots)]
        await self.engine.notifications.publish(
            'strm:%d:plan' % jid, '🧾 本次将按顺序同步 %d 个目录ID' % len(effective),
            '、'.join(effective), job_id=jid, category='strm_summary')
        batched = getattr(self.cloud, 'download_tree', None)
        fast = getattr(self.cloud, 'recursive_files_verified', None)
        completed_fast = False
        if callable(batched) or callable(fast):
            try:
                if callable(batched):
                    seen, files, stats = await self.download_manifest(jid, config, effective, paths)
                    self.store.event('sync', '已通过递归目录、文件及批量详情完成清单。')
                else:
                    seen, files, stats = await self.recursive_manifest(jid, config, effective, paths)
                    self.store.event('sync', '递归文件清单和目录路径核实完成。')
                completed_fast = True
            except CloudError as exc:
                if exc.code in FATAL_CODES:
                    raise
                self.store.event('sync', '递归扫描无法核实，回退逐目录扫描：' + str(exc))
            except (UncertainState, KeyError, TypeError, ValueError) as exc:
                self.store.event('sync', '递归扫描无法核实，回退逐目录扫描：' + str(exc))
        if not completed_fast:
            stack = [(cid, paths[cid], cid) for cid in reversed(effective)]
            seen, files = set(), {}
            stats = {}
            # 这份清单会授权后续的本地清理，因此目录读取带总量对账：截断即失败，
            # 不把「没读到」当成「云端没有」。
            read = getattr(self.cloud, 'children_verified', None) or self.cloud.children
            while stack:
                self.engine.boundary(jid)
                cid, path, root = stack.pop()
                if cid in seen: raise UncertainState('同步目录关系重复或循环，未发布清单。')
                seen.add(cid)
                if root not in stats:
                    synthetic={'_path':paths[root], '_root':root, 'name':'scope.strm'}
                    stats[root]={'id':root, 'name':paths[root][-1]['name'],
                                 'local_path':str(self.engine.root / Path(self.relative(synthetic,config)).parent),
                                 'directories':0, 'files':0, 'scan_seconds':0.0}
                    order=effective.index(root)+1
                    await self.engine.notifications.publish(f'strm:{jid}:scan-start:{root}','▶️ [%d/%d] 开始同步目录ID: %s' % (order, len(effective), root),
                        f'进度：{order}/{len(effective)}\n目录 ID：{root}\n云盘目录：{stats[root]["name"]}',
                        job_id=jid,category='strm_summary')
                started=time.monotonic()
                entries=await read(cid)
                stats[root]['scan_seconds']+=time.monotonic()-started
                stats[root]['directories']+=1
                for entry in entries:
                    self.engine.boundary(jid)
                    if entry['is_dir']:
                        stack.append((entry['id'], path + [{'id':entry['id'],'name':entry['name']}], root))
                    else:
                        if entry['id'] in files: raise UncertainState('同步清单出现重复文件 ID，未发布清单。')
                        files[entry['id']] = {**entry, '_path':path, '_root':root, '_scan_at':time.time()}
                        stats[root]['files']+=1
                self.engine.update_job(jid, stage=f'同步扫描：{len(seen)} 个目录，{len(files)} 个文件')
        rules = {**StrmOptions().model_dump(), **config}
        selected = [v for v in files.values() if not skip_reason(v, rules)]
        subtitles = [v for v in files.values() if Path(v['name']).suffix.lower() in SUBTITLES & set(rules['metadata_extensions'])]
        # Subtitle matching uses the same selected video set without creating STRM for subtitles.
        videos = bind_subtitles(selected + subtitles, rules['video_extensions'], SUBTITLES & set(rules['metadata_extensions']))
        config['sync_filtered_ids'] = [v['id'] for v in files.values() if skip_reason(v, rules)]
        bound_subtitles = {sub['id'] for video in videos for sub in video.get('subtitles', [])}
        config['sync_metadata'] = [v for v in files.values() if Path(v['name']).suffix.lower() in set(rules['metadata_extensions']) and v['id'] not in bound_subtitles]
        config['sync_filter_count'] = len([v for v in files.values() if Path(v['name']).suffix.lower() not in set(rules['metadata_extensions']) and skip_reason(v,rules)])
        # 把"按生成规则跳过"的文件名与原因都记下来：只给数字看不出是哪个文件、为什么跳过。
        skipped = [v for v in files.values()
                   if Path(v['name']).suffix.lower() not in set(rules['metadata_extensions'])
                   and skip_reason(v, rules)]
        config['sync_filter_names'] = [{'id': v['id'], 'name': v['name'],
                                        'size': v.get('size'), 'parent_id': v.get('parent_id'),
                                        'reason': skip_reason(v, rules)}
                                       for v in skipped[:10]]
        self.store.jobs.replace_manifest(jid, [(v['id'], dumps(v)) for v in videos])
        with self.store.db:
            account = self.engine.index.account(config)
            self.store.db.execute('INSERT OR REPLACE INTO sync_snapshots VALUES(?,?,?,?,?,?)',
                (account,self.scope(config),jid,time.time(),revision_at_start,dumps(paths)))
        current = json.loads(self.engine.job(jid)['config']);current.update({k:config[k] for k in ('sync_filtered_ids','sync_metadata','sync_filter_count')});current['sync_mode']='full'
        current['sync_directory_stats']=stats
        current['sync_effective_roots']=effective
        current['manifest_at']=time.time()
        current['manifest_started_at']=manifest_started_at
        current['manifest_revision']=revision_at_start
        self.engine.update_job(jid,config=dumps(current))

    def relative(self, source, config):
        path = source['_path']; ids = [x['id'] for x in path]
        archive = config.get('archive_cid')
        if archive and archive in ids:
            library = library_root(config, path)
            parts = [library] + [x['name'] for x in path[ids.index(archive)+1:]]
        else:
            root = source['_root']; pos = ids.index(root)
            parts = ['StrimKeep同步', component(path[pos]['name'],100) + '-' + root] + [x['name'] for x in path[pos+1:]]
        # Never silently turn separators into new path components.
        for name in parts + [source['name']]:
            if name in ('.','..') or '/' in name or '\\' in name or '\0' in name: raise ValueError('同步文件名含不安全路径字符。')
        return str(Path(*parts, Path(source['name']).stem + '.strm'))

    def mark_changed(self, jid):
        from .core import dumps
        config = json.loads(self.engine.job(jid)['config'])
        config['output_changed'] = True
        self.engine.update_job(jid, config=dumps(config))

    async def process(self, jid, row, config):
        from .core import atomic_write, dumps, UncertainState
        source, plan = json.loads(row['original']), json.loads(row['plan'])
        account = self.engine.index.account(config)
        # 清单即事实：本轮扫描刚用 children_verified 读过 id/name/size/sha1/pickcode，
        # 未变化的文件再发 file()+path_info() 只是重复劳动（每条两跳网络 + 限流），
        # 这正是全量同步慢的主因。仅在「扫描在 15 分钟内 + 指纹完全一致」时跳过回查。
        trusted, previous = False, None
        if config.get('trust_scan', True) and source.get('_scan_at'):
            old = self.engine.outputs.get(account, source['id'])
            if old and old['active']:
                previous = json.loads(old['source'])
                same = (str(previous.get('sha1', '')).upper() == str(source.get('sha1', '')).upper()
                        and previous.get('size') == source.get('size')
                        and previous.get('name') == source['name']
                        and previous.get('parent_id') == source['parent_id']
                        and json.loads(old['trail']) == [x['id'] for x in source['_path']]
                        and time.time() - source['_scan_at'] < 900)
                trusted = bool(same and self.engine.measurements.fingerprint(source))
        if trusted:
            actual = {'id': source['id'], 'name': source['name'], 'parent_id': source['parent_id'],
                      'size': source.get('size'), 'sha1': source.get('sha1', ''),
                      'pickcode': source.get('pickcode') or previous.get('pickcode', '')}
            current_path = source['_path']
        else:
            actual = await self.cloud.file(source['id'])
            self.engine.check_source(source, actual, source['name'], source['parent_id'])
            current_path = await self.cloud.path_info(source['parent_id'])
        if source['_root'] not in [p['id'] for p in current_path]:
            raise UncertainState('文件已移出本次同步目录，本文件暂不生成输出。')
        if current_path != source['_path']:
            source['_path'] = current_path
            self.engine.update_item(row['id'], original=dumps(source))
            # A changed directory name is rediscovered automatically. Do not
            # publish the old local path from a saved plan.
            if plan:
                plan['relative_path'] = self.relative(source, config)
        old = self.engine.outputs.get(account, source['id'])
        if not plan:
            relative = self.relative(source, config)
            plan = {'relative_path':relative, 'target_name':source['name'], 'warnings':[], 'local_files':{},
                    'previous_files':json.loads(old['files']) if old else {},
                    'previous_subtitles':json.loads(old['source']).get('subtitles',[]) if old else []}
            if old:
                retired = self.store.db.execute('SELECT revoked FROM playbacks WHERE id=?', (old['playback_id'],)).fetchone()
                if not old['active'] and retired and retired['revoked']:
                    # A previously retired source reappeared in the configured
                    # scope. Allocate a new identity; never reactivate its old
                    # explicitly revoked URL.
                    plan.update(playback_id=secrets.token_hex(16), token_cipher=self.store.crypto.encrypt(b'').decode(),
                                retired_playback_id=old['playback_id'])
                else:
                    plan.update(playback_id=old['playback_id'],token_cipher=old['token_cipher'])
            else:
                plan.update(playback_id=secrets.token_hex(16),token_cipher=self.store.crypto.encrypt(b'').decode())
            # A legacy organized token remains stable; bootstrap ownership from recorded hashes.
            if not old:
                managed = self.store.db.execute('SELECT i.plan,j.config FROM organized_files o JOIN organize_items i ON i.id=o.item_id JOIN organize_jobs j ON j.id=o.job_id WHERE o.file_id=?', (source['id'],)).fetchone()
                if managed and json.loads(managed['config']).get('account_id') == account:
                    legacy = json.loads(managed['plan'])
                    plan.update(playback_id=legacy['playback_id'],token_cipher=legacy['token_cipher'],previous_files=legacy.get('local_files',{}))
            self.engine.update_item(row['id'], plan=plan, state='planned', step='同步计划已持久化')
        pid = plan['playback_id']; token = self.store.crypto.decrypt(plan['token_cipher'].encode()).decode()
        mapping = self.store.db.execute('SELECT * FROM playbacks WHERE id=?', (pid,)).fetchone()
        if mapping and mapping['revoked']:
            self.engine.update_item(row['id'],state='skipped',step='保留已撤销播放入口',message='普通同步不会重新启用明确撤销的播放入口。')
            return
        if mapping and mapping['file_id'] != source['id']: raise UncertainState('同步播放映射身份冲突。')
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created) VALUES(?,?,?,?,?,?,?)', (pid,source['id'],actual['pickcode'],source['name'],plan['relative_path'],digest(token),time.time()))
        old_files = plan['previous_files']
        # 公共播放地址构造（B02）：同步不再写旧格式地址，统一为可读格式；
        # 可读段取 STRM 文件名主干（随改名反映当前名称），播放身份不变。
        identity = identity_of(actual) or identity_of(source)
        if identity:
            # 同步与整理写同一种地址：身份进 STRM，取链不查库。
            url = play115_url(config['playback_base'], *identity,
                              actual.get('name') or source['name']).encode()
        else:
            url = play_url(config['playback_base'], pid,
                           plan.get('target_name') or Path(plan['relative_path']).stem, token).encode()
        # Keep a verified legacy URL until its path or base actually changes.
        # Old tokenized URLs remain playable and need not all be rewritten.
        existing = self.engine.outputs.path(plan['relative_path'])
        if old and old['active'] and existing.is_file() and existing.stat().st_size <= 20*1024*1024:
            current = existing.read_bytes()
            # 历史地址（无扩展名等旧形式）只用于归属核对，不再作为可发布内容沿用；
            # 否则格式升级后旧 STRM 永远停在旧写法。
            if hashlib.sha256(current).hexdigest() == old_files.get(plan['relative_path']) and current == url:
                url = current
        content = {plan['relative_path']:url}
        warnings = []
        if source.get('subtitle_warning'): warnings.append('同主体多个视频的字幕归属不明，已保留原文件。')
        subtitle_cache = []
        subtitle_suffixes = [subtitle_suffix(source['name'], sub['name']) for sub in source['subtitles']]
        counts = {}
        for suffix in subtitle_suffixes:
            counts[suffix.casefold()] = counts.get(suffix.casefold(), 0) + 1
        for sub, suffix in zip(source['subtitles'], subtitle_suffixes):
            self.engine.boundary(jid)
            if counts[suffix.casefold()] > 1:
                warnings.append('多个字幕目标名称相同，保留旧输出并跳过冲突字幕。')
                continue
            relative = str(Path(plan['relative_path']).with_suffix('')) + suffix
            # 清单即事实：本轮扫描已经从批量详情拿到 id/name/size/sha1/pickcode，
            # 字幕不必再逐条回查（旧实现每个字幕两次 file()，138 个字幕就是 276 次请求，
            fresh = bool(sub.get('_scan_at')) and time.time() - sub['_scan_at'] < 900
            cached_id = fresh and self.engine.measurements.fingerprint(sub) and isinstance(sub.get('pickcode'), str) and sub['pickcode']
            try:
                if cached_id:
                    checked = {'id': sub['id'], 'name': sub['name'], 'parent_id': sub['parent_id'],
                               'size': sub['size'], 'sha1': sub['sha1'], 'pickcode': sub['pickcode']}
                else:
                    checked = await self.cloud.file(sub['id'])
                    self.engine.check_source(sub,checked,sub['name'],sub['parent_id'])
                previous = next((s for s in plan.get('previous_subtitles',[]) if s['id']==sub['id']),None)
                local = self.engine.outputs.path(relative)
                key = self.engine.measurements.fingerprint(sub)
                expected = old_files.get(relative) or plan['local_files'].get(relative)
                data = None
                if (key and previous and self.engine.measurements.fingerprint(previous)==key
                        and local.is_file() and local.stat().st_size <= 20*1024*1024
                        and hashlib.sha256(local.read_bytes()).hexdigest()==expected):
                    data = local.read_bytes()
                elif cached_id and not previous and old_files.get(relative) and local.is_file() and local.stat().st_size <= 20*1024*1024:
                    # Older registries may lack subtitle identities even though the
                    # local output is owned. Adopt only if both the ownership digest
                    # and the fresh 115 size/SHA1 prove it is this exact subtitle.
                    owner = self.store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
                    if owner and (owner['account_id'], owner['file_id'], owner['digest']) == (account, source['id'], old_files[relative]):
                        local_data = local.read_bytes()
                        if (len(local_data) == checked['size']
                                and hashlib.sha1(local_data).hexdigest().upper() == checked['sha1'].upper()
                                and hashlib.sha256(local_data).hexdigest() == old_files[relative]):
                            data = local_data
                if data is None:
                    link, headers = await self.cloud.url(checked['pickcode'],'StrimKeep/0.5 Subtitle')
                    data = await self.engine.download(link, {**headers,'User-Agent':'StrimKeep/0.5 Subtitle'},20*1024*1024)
                    if cached_id:
                        # 摘要一致就直接用；只有对不上才回云端核实一次（正常路径零请求）。
                        # 对不上不升级为告警：旧实现本来就只核对元数据、不核对字节，
                        # 这里保持一致的任务状态语义，只留一条事件便于排查。
                        if (len(data) != checked['size'] or
                                hashlib.sha1(data).hexdigest().upper() != checked['sha1'].upper()):
                            self.engine.check_source(sub,await self.cloud.file(sub['id']),sub['name'],sub['parent_id'])
                            self.store.event('sync', '字幕内容与清单摘要不一致，已按云端现状写入：'+sub['name'])
                    else:
                        self.engine.check_source(sub,await self.cloud.file(sub['id']),sub['name'],sub['parent_id'])
                subtitle_cache.append({k:sub[k] for k in ('id','name','parent_id','size','sha1') if k in sub})
                content[relative] = data
            except (httpx.HTTPError, ValueError, CloudError) as exc:
                if isinstance(exc, CloudError) and exc.code in ('LOGIN_REQUIRED','LOGIN_INVALID'):
                    raise
                warnings.append('外挂字幕下载失败，未清理本批旧输出。')
        # Preflight every destination before changing any local file.
        for relative in content:
            self.engine.outputs.guard(account,source['id'],relative,{**old_files,**plan['local_files']})
        # 可信路径不再二次回查：本轮扫描已经用 children_verified 证明了 id/name/size/sha1，
        # 这里写的是本地 STRM，云端事实不会因为本地写文件而改变。只有非可信路径
        # （扫描超过 15 分钟或指纹不符）才重新向 115 核实一次。
        if not trusted:
            self.engine.check_source(source,await self.cloud.file(source['id']),source['name'],source['parent_id'])
        for relative, data in content.items():
            self.engine.boundary(jid)
            path = self.engine.outputs.path(relative)
            checksum = hashlib.sha256(data).hexdigest()
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
                # Persist expected bytes before publishing: crash between write and registry is recoverable.
                plan['local_files'][relative] = checksum
                self.engine.update_item(row['id'],plan=plan,step='发布本地输出意图')
                self.mark_changed(jid)
                plan['output_changed']=True
                atomic_write(path,data,self.engine.root)
            plan['local_files'][relative] = checksum
        preserved = {rel:checksum for rel,checksum in old_files.items() if 'metadata_extensions' in config and Path(rel).suffix.lower() != '.strm' and Path(rel).suffix.lower() not in set(config['metadata_extensions'])}
        plan['local_files'].update(preserved)
        if not warnings:
            if set(old_files)-set(content)-set(preserved):
                self.mark_changed(jid)
                plan['output_changed']=True
            warnings.extend(self.engine.outputs.retire(jid,account,source['id'],old_files,set(content)|set(preserved)))
        else:
            plan['local_files'] = {**old_files,**plan['local_files']}
        plan['warnings'] = warnings
        self.engine.outputs.remember(account,{**actual,'subtitles':subtitle_cache},[x['id'] for x in source['_path']],plan)
        with self.store.db:
            self.store.db.execute('UPDATE playbacks SET name=?,relative_path=?,pickcode=? WHERE id=?', (source['name'],plan['relative_path'],actual['pickcode'],pid))
            retired_pid = plan.get('retired_playback_id')
            if retired_pid:
                self.store.db.execute('UPDATE organized_files SET playback_id=? WHERE file_id=? AND playback_id=?', (pid,source['id'],retired_pid))
            self.store.db.execute('UPDATE organized_files SET relative_path=? WHERE file_id=? AND playback_id=?', (plan['relative_path'],source['id'],pid))
            # Preserve metadata and any completed recycle proof while updating the latest managed output.
            managed = self.store.db.execute('SELECT i.id,i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=? AND o.playback_id=?',(source['id'],pid)).fetchone()
            if managed:
                saved = json.loads(managed['plan'])
                ids = [x['id'] for x in source['_path']]
                if config.get('archive_cid') in ids:
                    saved['parts'] = [x['name'] for x in source['_path'][ids.index(config['archive_cid'])+1:]]
                saved.pop('strm_filtered', None)
                saved.update(relative_path=plan['relative_path'],local_files=plan['local_files'],playback_base=config['playback_base'],target_name=source['name'],dest=source['parent_id'])
                if retired_pid:
                    saved.update(playback_id=pid,token_cipher=plan['token_cipher'])
                self.store.jobs.set_item_plan(managed['id'], saved)
        pending=json.loads(self.store.meta('library_name_pending') or '{}');migration_key=account+':'+source['id']
        if migration_key in pending:
            previous=pending.pop(migration_key)
            self.engine.outputs.mark_removed(jid,previous);self.mark_changed(jid);plan['output_changed']=True
            with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO meta VALUES('library_name_pending',?)",(json.dumps(pending,ensure_ascii=False),))
        self.engine.update_item(row['id'],plan=plan,state='done',step='STRM 与字幕已核实同步',message='；'.join(warnings))

    async def backfill_sidecars(self, jid, row, config):
        """Supplement missing metadata without replacing existing sidecars or STRM."""
        from .metadata_files import write_sidecars, _show_dir
        from .media import parse_name
        settings = config.get('strm_options') if isinstance(config.get('strm_options'), dict) else config
        current = self.store.db.execute('SELECT * FROM organize_items WHERE id=?', (row['id'],)).fetchone()
        if not current or current['state'] != 'done': return
        plan = json.loads(current['plan'] or '{}')
        original_plan = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        relative = plan.get('relative_path')
        if not relative: return
        target = self.engine.outputs.path(relative)
        source = json.loads(current['original'])
        missing_media = settings.get('emby_media_info') and not target.with_name(target.stem+'-media-info.json').exists()
        folder = _show_dir(target)
        want_nfo = settings.get('generate_nfo') and (not target.with_suffix('.nfo').exists() or (folder != target.parent and not (folder/'tvshow.nfo').exists()))
        want_images = settings.get('generate_images') and (not (folder/'poster.jpg').exists() or not (folder/'fanart.jpg').exists())
        if want_nfo or want_images:
            try:
                legacy = self.store.db.execute('SELECT i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=?', (source['id'],)).fetchone()
                meta = plan if plan.get('details') else (json.loads(legacy['plan']) if legacy and legacy['plan'] else {})
                if not meta.get('details'):
                    hint = parse_name(source['name'], '/'.join(p['name'] for p in source.get('_path', [])))
                    meta = {'hint':hint, 'details':await self.engine.identify(hint), 'episodes':[]}
                written = await write_sidecars(self.engine, target, meta, settings, missing_only=True)
                if written:
                    plan['output_changed'] = True
                    self.mark_changed(jid)
            except Exception as exc:
                self.store.event('sync', '旧库附属文件补齐未完成：%s（%s）' % (source.get('name',''), type(exc).__name__))
                plan.setdefault('warnings', []).append('NFO/海报补齐未完成，可查看同步日志后重试。')
        if missing_media and config.get('notify_emby', True):
            plan['output_changed'] = True
            self.mark_changed(jid)
        elif missing_media:
            plan.setdefault('warnings', []).append('媒体信息补齐需要启用 Emby 通知，当前未提交。')
        # Do not write the same plan a second time for every already complete item.
        if json.dumps(plan, sort_keys=True, ensure_ascii=False) != original_plan:
            self.engine.update_item(row['id'], plan=plan)

    async def stale_check(self, jid, config):
        """全量扫描后的失效差集：本地 strm 在、云端源没了 → 只标记不删除。"""
        if not config.get('stale_detect'):
            return
        account = self.engine.index.account(config)
        sources = set(config.get('source_cids') or [])
        missing = []
        listings = {}
        for old in self.store.db.execute('SELECT * FROM output_registry WHERE account_id=? AND active=1', (account,)).fetchall():
            trail = json.loads(old['trail'])
            if not sources.intersection(trail):
                continue
            # 先看该文件所在目录的清单（同一次同步里已读过、有缓存）：命中即「源文件还在」。
            # 以前每个台账行都打一次存在性查询，库一大就是几千次请求。
            parent = str(trail[-1]) if trail else ''
            if parent and hasattr(self.cloud, 'children'):
                if parent not in listings:
                    try:
                        listings[parent] = {str(entry.get('id')) for entry in await self.cloud.children(parent)}
                    except CloudError:
                        listings[parent] = None      # 目录读不到：退回逐条确认，不据此判失效
                ids = listings[parent]
                if ids is not None and str(old['file_id']) in ids:
                    continue
            if await self.cloud.live_file_exists(old['file_id']):
                continue
            for rel in json.loads(old['files']):
                if rel.lower().endswith('.strm'):
                    missing.append({'account_id': account, 'file_id': old['file_id'], 'relative_path': rel})
        dedup = {json.dumps(m, ensure_ascii=False, sort_keys=True) for m in missing}
        missing = [json.loads(x) for x in sorted(dedup)]
        previous = self.store.get('stale_strms') or '[]'
        current = json.dumps(missing, ensure_ascii=False)
        self.store.set_many({'stale_strms': current})
        # 同一批失效重复出现时不再每轮都写一条：只在结果变化时提醒。
        if missing and current != previous:
            self.store.event('strm_sync', '失效 STRM 检测：发现 %d 条本地 strm 的网盘源文件已消失，已在 STRM 管理页列出（只标记未删除）。' % len(missing))

    async def stale_cleanup(self):
        """Legacy manual cleanup: require current account, owner and unchanged bytes."""
        stale = json.loads(self.store.get('stale_strms') or '[]')
        account = self.engine.index.account({})
        cleaned, rest = [], []
        for item in stale:
            rel, fid = item.get('relative_path', ''), item.get('file_id', '')
            try:
                if not fid or (item.get('account_id') and item['account_id'] != account):
                    rest.append({**item, 'error': '账号或文件身份不匹配，已保留。'}); continue
                row = self.store.db.execute('SELECT * FROM output_registry WHERE account_id=? AND file_id=? AND active=1', (account, fid)).fetchone()
                owner = self.store.db.execute('SELECT account_id,file_id,digest FROM owned_outputs WHERE relative_path=?', (rel,)).fetchone()
                files = json.loads(row['files']) if row else {}
                expected = files.get(rel) if isinstance(files, dict) else None
                if not expected or not owner or (owner['account_id'],owner['file_id'],owner['digest']) != (account,fid,expected):
                    rest.append({**item, 'error': '本地文件归属无法核实，已保留。'}); continue
                local = self.engine.outputs.path(rel)
                if local.is_symlink() or (local.exists() and (not local.is_file() or local.stat().st_size > 20*1024*1024 or hashlib.sha256(local.read_bytes()).hexdigest() != expected)):
                    rest.append({**item, 'error': '本地文件已修改或路径不安全，已保留。'}); continue
                if await self.cloud.live_file_exists(fid):
                    rest.append({**item, 'error': '网盘源文件仍存在，已保留。'}); continue
                if local.is_symlink() or (local.exists() and (not local.is_file() or local.stat().st_size > 20*1024*1024 or hashlib.sha256(local.read_bytes()).hexdigest() != expected)):
                    rest.append({**item, 'error': '核查期间本地文件发生变化，已保留。'}); continue
                if local.is_file():
                    from .internal_delete import remember
                    if not remember(self.store, [rel]):
                        raise RuntimeError('内部删除登记失败，已保留本地文件。')
                    local.unlink()
                with self.store.db:
                    self.store.db.execute('DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?', (rel,account,fid))
                    self.store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=? AND file_id=?', (row['playback_id'],fid))
                    self.store.db.execute('DELETE FROM organized_files WHERE file_id=? AND playback_id=?', (fid,row['playback_id']))
                    self.store.db.execute('UPDATE output_registry SET active=0 WHERE account_id=? AND file_id=?', (account,fid))
                cleaned.append(rel)
            except Exception as exc:
                rest.append({**item, 'error': str(exc)[:80]})
        self.store.set_many({'stale_strms': json.dumps(rest, ensure_ascii=False)})
        return cleaned, rest

    async def cleanup(self, jid, config, after=None):
        from .core import UncertainState
        account = self.engine.index.account(config); roots = set(config['source_cids'])
        present = {r['file_id'] for r in self.store.db.execute('SELECT file_id FROM organize_items WHERE job_id=?',(jid,))} | set(config.get('sync_filtered_ids', []))
        removed, warnings = 0, []
        started = float(after or 0)
        retired = 0
        def preflight(old):
            # A group is all-or-nothing: don't remove a subtitle first and then
            # discover that the STRM belongs to a different owner or was edited.
            for relative, expected in json.loads(old['files']).items():
                local = self.engine.outputs.path(relative)
                owner = self.store.db.execute('SELECT account_id,file_id,digest FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
                if not owner or (owner['account_id'],owner['file_id'],owner['digest']) != (account,old['file_id'],expected):
                    return '已保留归属不明的本地文件：' + relative
                if local.is_symlink():
                    return '已保留符号链接：' + relative
                if local.exists() and (not local.is_file() or local.stat().st_size > 20*1024*1024 or hashlib.sha256(local.read_bytes()).hexdigest() != expected):
                    return '已保留被修改的本地文件：' + relative
            return ''
        for old in self.store.db.execute('SELECT * FROM output_registry WHERE account_id=? AND active=1',(account,)).fetchall():
            if old['file_id'] in present: continue
            if not roots.intersection(json.loads(old['trail'])):
                # 范围外的旧台账（换过同步源/库根改名留下的）：STRM 已经不在本地就停用。
                # 否则巡检天天把它算成「需要修复」，而清理又按范围跳过，永远解不开。
                files = json.loads(old['files'])
                if not any(f.endswith('.strm') and self.engine.outputs.path(f).is_file() for f in files):
                    with self.store.db:
                        self.store.db.execute('UPDATE output_registry SET active=0 WHERE account_id=? AND file_id=?',
                                              (account, old['file_id']))
                    retired += 1
                continue
            if started and float(old['updated']) > started:
                # 扫描开始之后才登记的输出来不及进这份清单，不能据此判定失效。
                continue
            self.engine.boundary(jid)
            problem = preflight(old)
            if problem:
                warnings.append(problem)
                continue
            if await self.cloud.live_file_exists(old['file_id']):
                actual = await self.cloud.file(old['file_id'])
                trail = await self.cloud.directory(actual['parent_id'])
                if roots.intersection(trail): raise UncertainState('完整清单与当前范围不一致，已停止清理；请重新同步。')
                # No longer in the selected source: retire only this scope's
                # old local output, keeping the live playback mapping usable.
                item_warnings = self.engine.outputs.retire(jid,account,old['file_id'],json.loads(old['files']))
                warnings.extend(item_warnings)
                if item_warnings:
                    continue
                with self.store.db:self.store.db.execute('UPDATE output_registry SET active=0 WHERE account_id=? AND file_id=?',(account,old['file_id']))
                self.mark_changed(jid);removed+=1
                continue
            # Only retire bytes that still match the registered owner and digest.
            item_warnings = self.engine.outputs.retire(jid,account,old['file_id'],json.loads(old['files']))
            warnings.extend(item_warnings)
            if item_warnings:
                continue
            self.mark_changed(jid)
            with self.store.db:
                # Auto-cleanup only retires the output. Explicitly revoked
                # playback identities remain revoked; an untouched identity
                # can be reused if its source returns from the recycle bin.
                self.store.db.execute('UPDATE output_registry SET active=0 WHERE account_id=? AND file_id=?',(account,old['file_id']))
            removed += 1
        # Unregistered STRM has no verified 115 identity; do not infer that
        # a missing manifest entry authorizes its removal.
        if retired:
            self.store.event('strm_sync', f'停用 {retired} 条范围外、本地已不存在的旧台账（不删任何文件）。')
        current=json.loads(self.engine.job(jid)['config']);current['sync_removed']=removed
        self.engine.update_job(jid,config=json.dumps(current,ensure_ascii=False))
        return removed,warnings

    async def run(self, jid, config, sweep=None):
        if hasattr(self.cloud, 'use_scope'):
            self.cloud.use_scope(self.engine.current_account(), jid)
        if hasattr(self.cloud, 'reset_ledger'):
            self.cloud.reset_ledger()
        started = time.time()
        try:
            await self._run(jid, config, sweep=sweep)
            if config.get('stale_detect'):
                # 全量扫描后做差集（只标记不删除）。这条链路以前只有测试在调，
                # 打开开关也不会执行——现在真的接进同步流程。
                await self.stale_check(jid, config)
            # 这个时间点只在本次真正跑完之后才更新，而且记的是**开始**时刻：
            # 扫描期间发生的改动下一轮还会被看到，不会漏。
            if str(self.engine.job(jid)['state'] or '') in ('completed', 'partial'):
                # 除了全局时间，还按同步目录各记一份：多根同步时，某个根没跑过就
                # 不能拿另一个根的时间去跳过它。
                previous = self.store.meta_json('sync_watermark', {}) or {}
                roots_map = dict(previous.get('roots') or {})
                for cid in (config.get('source_cids') or []):
                    roots_map[str(cid)] = started
                self.store.set_meta('sync_watermark', {
                    'at': started, 'job': jid,
                    'mode': 'incremental' if config.get('incremental_since') else 'full',
                    'roots': roots_map})
        finally:
            if hasattr(self.cloud, 'use_scope'):
                self.cloud.use_scope(self.engine.current_account(), jid)
            ledger = self.cloud.ledger_summary() if hasattr(self.cloud, 'ledger_summary') else None
            if ledger:
                saved = json.loads(self.engine.job(jid)['config'])
                saved['request_ledger'] = ledger
                self.engine.update_job(jid, config=json.dumps(saved, ensure_ascii=False))
                self.store.event('strm_sync', f'📊 同步请求账本：{ledger["text"]}')
            await self.hash_worker.close()

    def plain_token(self, cipher):
        if cipher not in self._tokens:
            if len(self._tokens) > 512:
                self._tokens.clear()
            self._tokens[cipher] = self.store.crypto.decrypt(cipher.encode()).decode()
        return self._tokens[cipher]

    def heal_stale_paths(self, row):
        """库根改名/迁移会留下「旧路径 + 新路径指向同一份内容」的台账。

        旧路径的文件早就不在了，但新路径的摘要一模一样：把旧键剪掉、主路径改指新键，
        否则巡检会一直把它当「需要修复」，而清理又因为文件在清单里跳过。
        """
        try:
            files = json.loads(row['files'])
        except Exception:
            self.notice('heal', '转存清单解析失败，已跳过该行修复（巡检会继续标记待修复）。')
            return row
        existing = {p: d for p, d in files.items() if self.engine.outputs.path(p).is_file()}
        stale = [p for p in files if p not in existing]
        if not stale:
            return row
        by_digest, by_name = {}, {}
        for path, digest_value in existing.items():
            by_digest.setdefault(digest_value, path)
            by_name.setdefault(Path(path).name, path)
        changed = False
        for path in stale:
            # 先认同摘要；改名/换口径后内容会变，再认同名文件（同一行里同名就是同一个输出）。
            twin = by_digest.get(files[path]) or by_name.get(Path(path).name)
            if not twin or twin == path:
                continue
            files.pop(path, None)
            files[twin] = hashlib.sha256(self.engine.outputs.path(twin).read_bytes()).hexdigest()
            with self.store.db:
                self.store.db.execute('DELETE FROM owned_outputs WHERE relative_path=?', (path,))
                self.store.db.execute('UPDATE owned_outputs SET digest=? WHERE relative_path=?',
                                      (files[twin], twin))
            changed = True
        if not changed:
            return row
        relative = row['relative_path']
        if relative not in files:
            for path, digest_value in files.items():
                if digest_value == json.loads(row['files']).get(relative):
                    relative = path
                    break
        with self.store.db:
            self.store.db.execute(
                'UPDATE output_registry SET files=?,relative_path=? WHERE account_id=? AND file_id=?',
                (json.dumps(files, ensure_ascii=False), relative, row['account_id'], row['file_id']))
        self.store.event('strm_sync', '台账路径自愈：%s -> %s' % (row['relative_path'][:60], relative[:60]))
        return self.store.db.execute(
            'SELECT * FROM output_registry WHERE account_id=? AND file_id=?',
            (row['account_id'], row['file_id'])).fetchone()

    def known_forms(self, row, config, relative, source):
        """这台播放身份历史上可能写出的全部字节形态（含带令牌的旧地址）。"""
        name = source.get('name', '') or Path(relative).stem
        forms = candidates(config['playback_base'], row['playback_id'], name)
        forms += candidates(config['playback_base'], row['playback_id'], Path(relative).stem)
        # 同一份输出的两种地址形态都算已知写法：旧文件不必为换格式被反复重写。
        forms += play115_candidates(config['playback_base'], source, name)
        forms += play115_candidates(config['playback_base'], source, Path(relative).stem)
        try:
            token = self.plain_token(row['token_cipher'])
        except Exception:
            token = ''
        if token:
            forms += candidates(config['playback_base'], row['playback_id'], name, token)
            forms += candidates(config['playback_base'], row['playback_id'], Path(relative).stem, token)
        return {hashlib.sha256(content).hexdigest() for content in forms}

    async def inspect_output(self, row, config):
        """本地输出诊断：回答「这份输出还能不能就地修好」，全程不碰云端。

        返回 (verdict, detail)：
        - ok      文件、归属、播放映射都对得上；
        - missing 主 STRM 不见了，按当前配置重写即可恢复；
        - content 主 STRM 不是任何已知写法（换过播放地址，或被外部改过），重写即可恢复；
        - sidecar 字幕等附属文件缺失或损坏，本地造不出来，必须回云端补；
        - owner   本地路径登记在别的文件名下，覆盖会串库；
        - mapping 播放映射缺失、已撤销或身份不符；
        - broken  台账读不出来、主路径缺失或路径不安全。

        和 unchanged() 的区别：台账里没存 _path，这里也不做「云端路径是否被移动」的判断——
        那是完全核对的事。巡检只回答一个问题：这份本地 STRM 还能不能用。
        """
        relative = row['relative_path']
        try:
            source, files = json.loads(row['source']), json.loads(row['files'])
        except Exception:
            return 'broken', {'relative_path': relative}
        if Path(relative).suffix.lower() != '.strm' or relative not in files:
            return 'broken', {'relative_path': relative}
        mapping = self.store.db.execute('SELECT * FROM playbacks WHERE id=?', (row['playback_id'],)).fetchone()
        if not mapping or mapping['revoked'] or mapping['file_id'] != row['file_id']:
            return 'mapping', {'relative_path': relative}
        paths = {}
        for name in files:
            try:
                paths[name] = self.engine.outputs.path(name)
            except Exception:
                return 'broken', {'relative_path': relative, 'file': name}
            owner = self.store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?', (name,)).fetchone()
            if not owner or (owner['account_id'], owner['file_id']) != (row['account_id'], row['file_id']):
                return 'owner', {'relative_path': relative, 'file': name}
            if owner['digest'] != files[name]:
                # 归属指纹过期：主 STRM 可以重写刷新，附属文件只能重新下载。
                return ('content' if name == relative else 'owner'), {'relative_path': relative, 'file': name}
        if files[relative] not in self.known_forms(row, config, relative, source):
            return 'content', {'relative_path': relative, 'reason': '播放地址口径已变'}
        for name, checksum in files.items():
            local = paths[name]
            try:
                local.stat()
            except OSError:
                return ('missing' if name == relative else 'sidecar'), {'relative_path': relative, 'file': name}
            # 0.9.54：不再用 output_stats 缓存摘要。STRM 只有几百字节，哈希成本可以忽略，
            # 而那张缓存表要求每次搬文件都同步，得不偿失。
            try:
                actual = await self.hash_worker.checksum(local)
            except Exception:
                return 'broken', {'relative_path': relative, 'file': name}
            if actual != checksum:
                return ('content' if name == relative else 'sidecar'), {'relative_path': relative, 'file': name}
        return 'ok', {'relative_path': relative, 'sync_unchanged': True}

    def heal_output(self, jid, row, config):
        """就地重写一份完全由本地配置决定的 STRM：不枚举、不读云端。

        STRM 内容只由 playback_base、播放身份和名称决定（candidates 的第一个形态），
        所以缺失或口径变旧都能当场补回来。字幕这类需要下载的附属文件不在自愈范围，
        归属冲突也不能覆盖——这两种交给完全核对。
        """
        from .core import atomic_write
        relative = row['relative_path']
        try:
            source = json.loads(row['source'])
        except Exception:
            return False
        owner = self.store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
        if owner and (owner['account_id'], owner['file_id']) != (row['account_id'], row['file_id']):
            return False
        name = source.get('name', '') or Path(relative).stem
        # 这一步全在本地算，不需要为了拿身份回云端读一次。
        forms = play115_candidates(config['playback_base'], source, name)
        content = forms[0] if forms else candidates(config['playback_base'], row['playback_id'], name)[0]
        checksum = hashlib.sha256(content).hexdigest()
        path = self.engine.outputs.path(relative)
        atomic_write(path, content, self.engine.root)
        files = json.loads(row['files'])
        files[relative] = checksum
        with self.store.db:
            self.store.db.execute('UPDATE output_registry SET files=? WHERE account_id=? AND file_id=?',
                                  (json.dumps(files, ensure_ascii=False), row['account_id'], row['file_id']))
            self.store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                                  (relative, row['account_id'], row['file_id'], checksum))
        self.engine.audit(jid, 0, '本地自愈：按当前播放地址重写 STRM：' + relative)
        return True

    async def verify_output(self, row, config):
        """兼容入口：只回答「这份输出是否完全正常」。"""
        verdict, detail = await self.inspect_output(row, config)
        return detail if verdict == 'ok' else None

    SWEEP_PROGRESS_SECONDS = 1.0

    SWEEP_PROGRESS_SECONDS = 1.0

    def should_sweep(self, config):
        """同步一律本地巡检（对齐）；只有显式勾选 full_scan 或内部强制才枚举云端。"""
        if config.get('_force_full') or config.get('full_scan'):
            return False
        flag = self.store.get('sync_sweep_enabled')
        if flag not in (None, '') and not int(flag):
            return False
        if self.store.get('sync_full_due'):
            self.store.set_many({'sync_full_due': 0})
            self.store.event('sync', '上一轮巡检发现待修复输出，本轮升级为完全核对。')
            return False
        account = self.engine.index.account(config)
        count = self.store.db.execute(
            'SELECT count(*) FROM output_registry WHERE account_id=? AND active=1', (account,)).fetchone()[0]
        return count > 0

    def sweep_orphans(self, jid, config, registered):
        """扫本地文件树，处理台账之外的 .strm。

        只删「能证明是旧副本或已失效」的：

        - 旧形态 ``/play/<token>``：播放身份不存在或已撤销 → 删（原行为）；
        - 新形态 ``/play115/<pickcode>/<fid>/<sha1>/<size>/<name>``：地址里带着 file_id。
          若同一个云端文件已在本账号下登记到**另一个**路径，且那份文件确实还在 →
          这一条就是旧命名留下的副本 → 删；登记不到、或登记的正是自己 → 只报数，不猜。

        0.8.31 之前这里只认 ``/play/`` 前缀，而 0.7.12 起 STRM 写的都是 play115 形态，
        于是「本地多余的 STRM」再也没被清理过（现场遗留 395 个旧命名副本）。
        返回 (删除数, 保留数)。
        """
        base = (config.get('playback_base') or '').rstrip('/')
        legacy_prefix = base + '/play/'
        identity_prefix = base + '/play115/'
        account = self.engine.index.account(config)
        removed = kept = 0
        for local in self.engine.root.rglob('*.strm'):
            relative = local.relative_to(self.engine.root).as_posix()
            if relative in registered:
                continue
            try:
                head = local.read_text('utf-8', 'ignore')[:400].strip()
            except OSError:
                kept += 1
                continue
            if head.startswith(identity_prefix):
                doomed = self._duplicate_output(account, relative, head[len(identity_prefix):])
            elif head.startswith(legacy_prefix):
                item = head[len(legacy_prefix):].split('/', 1)[0].split('?', 1)[0].strip()
                row = self.store.db.execute('SELECT revoked FROM playbacks WHERE id=?', (item,)).fetchone()
                doomed = not (row and not row['revoked'])
            else:
                kept += 1
                continue
            if not doomed:
                kept += 1
                continue
            self.engine.outputs.mark_removed(jid, relative)
            try:
                # 联动清同名侧车；目录已无视频时再删空目录。删不掉就保留并计入 kept。
                self.engine.outputs.unlink_recorded(jid, relative, local, purge=True)
            except (OSError, RuntimeError):
                kept += 1
                continue
            if local.exists():
                kept += 1
                continue
            with self.store.db:
                self.store.db.execute('DELETE FROM owned_outputs WHERE relative_path=?', (relative,))
            self.engine.audit(jid, 0, '清理旧命名或已失效的本地 STRM：' + relative)
            removed += 1
        return removed, kept

    def _duplicate_output(self, account, relative, tail):
        """play115 地址里的 file_id 是否已被登记到别的、且仍存在的路径。"""
        parts = tail.split('/')
        if len(parts) < 2:
            return False
        row = self.store.db.execute(
            'SELECT relative_path,files,active FROM output_registry WHERE account_id=? AND file_id=?',
            (account, parts[1])).fetchone()
        if not row or not row['active']:
            return False
        try:
            files = json.loads(row['files'])
        except Exception:
            return False
        canonical = row['relative_path']
        if not canonical or canonical == relative or relative in files:
            return False
        # 登记的那份必须真的还在，否则删掉这一条等于把唯一的输出删了。
        return self.engine.outputs.path(canonical).is_file()

    async def sweep(self, jid, config):
        """本地巡检：只核对已登记的 STRM 输出，不枚举云端、不碰云端。

        对齐每天 00:00 那一轮：自己能修的（主 STRM 缺失、内容口径变旧）就地重写，
        不需要枚举云端；只有本地修不了的（字幕缺失、归属冲突、播放映射断裂）才置
        sync_full_due，下一轮自动升级为完全核对。
        顺带清掉已撤销的本地输出（只删明确撤销的，未登记的一律只报告）。
        """
        from .core import dumps
        account = self.engine.index.account(config)
        rows = self.store.db.execute(
            'SELECT * FROM output_registry WHERE account_id=? AND active=1 ORDER BY relative_path',
            (account,)).fetchall()
        total = len(rows)
        pending = json.loads(self.store.meta('library_name_pending') or '{}')
        ok = broken = healed = skipped = 0
        problems = []
        revoked = []          # [(row, files)]：已撤销，直接清掉并停用台账
        registered = set()    # 有效台账登记的本地文件
        reported = time.monotonic()
        current = json.loads(self.engine.job(jid)['config'])
        current['sync_mode'] = 'sweep'
        self.engine.update_job(jid, config=dumps(current), stage=f'本地巡检：{total} 条输出', message='')
        for row in rows:
            self.engine.boundary(jid)
            if account + ':' + row['file_id'] in pending:
                skipped += 1
                continue
            try:
                files = json.loads(row['files'])
            except Exception:
                broken += 1
                problems.append(row['relative_path'])
                continue
            dead = self.store.db.execute(
                'SELECT 1 FROM playbacks WHERE id=? AND revoked=1', (row['playback_id'],)).fetchone()
            if dead:
                revoked.append((row, files))
                continue
            row = self.heal_stale_paths(row)
            files = json.loads(row['files'])
            registered.update(files.keys())
            # 巡检只读，不抢 publication_gate：整理搬文件时会长时间占用它，
            # 逐条去等就等于把巡检拖成分钟级。
            verdict, detail = await self.inspect_output(row, config)
            if verdict == 'ok':
                ok += 1
            elif verdict in ('missing', 'content') and self.heal_output(jid, row, config):
                healed += 1
            else:
                broken += 1
                problems.append(row['relative_path'])
            now = time.monotonic()
            if now - reported >= self.SWEEP_PROGRESS_SECONDS:
                reported = now
                self.engine.update_job(jid, stage=f'本地巡检：{ok + healed + broken}/{total}，自愈 {healed}，待修复 {broken}')
        removed = 0
        if revoked:
            from .internal_delete import remember
            from .fsops import purge_strm
            if not remember(self.store, [name for _, files in revoked for name in files]):
                raise RuntimeError('内部删除登记失败，已保留本地文件。')
            for row, files in revoked:
                gone = []
                for name in files:
                    path = self.engine.root / name
                    try:
                        if Path(name).suffix.lower() == '.strm':
                            # 联动清同名侧车；目录已无视频时再删空目录。
                            purge_strm(path, self.engine.root, store=self.store)
                        elif path.exists():
                            path.unlink()
                    except (OSError, RuntimeError):
                        continue
                    if not path.exists():
                        gone.append(name)
                if not gone:
                    continue          # 一个都没删掉：台账保留，下一轮巡检再试
                with self.store.db:
                    for name in gone:
                        self.store.db.execute('DELETE FROM owned_outputs WHERE relative_path=?', (name,))
                    self.store.db.execute(
                        'UPDATE output_registry SET active=0 WHERE account_id=? AND file_id=?',
                        (row['account_id'], row['file_id']))
                removed += len(gone)
        # 其余未登记项只报数，留给完全核对。
        removed_orphans, kept_orphans = self.sweep_orphans(jid, config, registered)
        tail = f'；跳过 {skipped} 条（媒体库改名进行中）' if skipped else ''
        if removed:
            tail += f'；清理已撤销输出 {removed} 条'
        if removed_orphans:
            tail += f'；清理播不了的 STRM {removed_orphans} 条'
        if kept_orphans:
            tail += f'；{kept_orphans} 条未登记 STRM 未动（等完全核对）'
        if broken:
            # 同一批问题只升级一次：否则完全核对没修好（或没跑完）时，每天都会白跑一次全量。
            signature = hashlib.sha256('|'.join(sorted(problems)).encode()).hexdigest()[:16]
            previous = self.store.get('sync_sweep_signature') or ''
            if signature != previous:
                self.store.set_many({'sync_full_due': 1, 'sync_sweep_signature': signature})
                advice = '下一轮自动做完全核对'
            else:
                advice = '与上一轮相同，已登记；需要时手动做一次完全核对'
            healed_text = f'，就地自愈 {healed} 条' if healed else ''
            message = (f'本地巡检：{ok} 条正常{healed_text}，{broken} 条输出需要修复（{advice}）。'
                       f'未枚举云端，未执行失效清理{tail}。首个：{problems[0][:80]}')
            state = 'partial'
        else:
            if self.store.get('sync_sweep_signature'):
                self.store.set_many({'sync_sweep_signature': ''})
            healed_text = f'，就地自愈 {healed} 条' if healed else ''
            message = f'本地巡检：{ok} 条输出正常{healed_text}；未枚举云端，未执行失效清理{tail}。'
            state = 'completed'
        self.engine.update_job(jid, state=state, stage='STRM 巡检结束', message=message)
        self.store.event('strm_sync', message)
        return state

    async def _run(self, jid, config, sweep=None):
        from .core import UncertainState
        self.engine.boundary(jid)
        # 定时同步默认走本地巡检：不枚举云端，坏输出就地修。
        if sweep is None:
            sweep = self.should_sweep(config)
        if sweep:
            return await self.sweep(jid, config)
        job = self.engine.job(jid)
        if not job['manifest']:
            labels=config.get('directory_labels') or {}
            roots=config.get('source_cids') or []
            await self.engine.notifications.publish(f'strm:{jid}:start','📋 STRM 同步开始',
                f'本次选择 {len(roots)} 个目录：\n'+'\n'.join(f'{i}. {labels.get(cid,cid)} · ID {cid}' for i,cid in enumerate(roots,1)),
                job_id=jid,category='strm_summary')
            # 手动和定时同步都必须发现网盘新增、改名与移动；仅在整理后
            # 的目标目录增量任务缩小范围，普通同步不使用旧快照或本地巡检。
            if self.engine.scan_receive_gate.locked():
                self.engine.update_job(jid, stage='等待目录扫描或转存核对')
            async with self.engine.scan_receive_gate:
                # Import proven historical ownership before the scan boundary.
                # Skip extra root lookups when the ownership registry is complete.
                account = self.engine.index.account(config)
                missing_history = self.store.db.execute(
                    'SELECT 1 FROM organized_files o LEFT JOIN output_registry r ON r.file_id=o.file_id AND r.account_id=? WHERE r.file_id IS NULL LIMIT 1', (account,)).fetchone()
                if missing_history:
                    historical_paths = {cid: await self.cloud.path_info(cid) for cid in roots}
                    async with self.engine.publication_gate:
                        await self.engine.outputs.import_managed(jid,account,historical_paths)
                await self.manifest(jid,config)
        else:
            # 续跑的清单可能已经是几小时前的：期间库里新增/移动过的文件不在其中，
            # 拿它去授权清理会被正确拦下（也等于永远做不完）。过期即重建。
            saved = json.loads(job['config'])
            stamp = saved.get('manifest_at') or job['updated']
            if saved.get('sync_mode') == 'cached' or time.time() - float(stamp or 0) > self.FULL_SCAN_SECONDS:
                self.engine.update_job(jid, manifest=0, stage='清单已过期，重新读取云端目录')
                if self.engine.scan_receive_gate.locked():
                    self.engine.update_job(jid, stage='等待目录扫描或转存核对')
                async with self.engine.scan_receive_gate:
                    await self.manifest(jid, config)
        saved_config = json.loads(self.engine.job(jid)['config'])
        config.update({k:saved_config[k] for k in ('sync_filtered_ids','sync_metadata','sync_filter_count') if k in saved_config})
        cached = saved_config.get('sync_mode') == 'cached'
        paths = {cid:await self.cloud.path_info(cid) for cid in config['source_cids']}
        async with self.engine.publication_gate:
            await self.engine.outputs.import_managed(jid,self.engine.index.account(config),paths)
        next_progress = 0.0
        for row in self.store.db.execute("SELECT * FROM organize_items WHERE job_id=? AND state NOT IN ('done','skipped') ORDER BY id",(jid,)).fetchall():
            self.engine.boundary(jid)
            now = time.monotonic()
            if now >= next_progress:
                self.engine.update_job(jid,stage='同步：'+json.loads(row['original'])['name'])
                next_progress = now + 1.0
            async with self.engine.publication_gate:
                plan = None if config.get('_force_full') else await self.unchanged(json.loads(row['original']), config)
                self.engine.boundary(jid)
                if plan:
                    repair = plan.pop('_sync_trail_repair', None)
                    # Promote only fresh, complete scan evidence; don't rewrite a
                    # registry record created or changed during the scan.
                    started = saved_config.get('manifest_started_at')
                    if (repair and saved_config.get('sync_mode') == 'full' and started
                            and saved_config.get('manifest_revision') == self.store.meta(
                                'sync_revision:' + self.engine.index.account(config))):
                        with self.store.db:
                            self.store.db.execute(
                                'UPDATE output_registry SET trail=? WHERE account_id=? AND file_id=? AND trail=? AND updated<=?',
                                (json.dumps(repair['new'], ensure_ascii=False, separators=(',', ':')),
                                 self.engine.index.account(config), row['file_id'], repair['old'], started))
                    self.engine.update_item(row['id'],plan=plan,state='done',step='输出未变更，已复用核对结果')
            if plan:
                await self.backfill_sidecars(jid, row, config)
                continue
            elif cached:
                # The local file changed after cache preflight. Do not mutate from
                # an old listing: resume will rebuild a fresh cloud manifest.
                self.invalidate(self.engine.index.account(config))
                self.engine.update_job(jid,manifest=0)
                await self._run(jid, config)
                return
            else:
                try:
                    async with self.engine.publication_gate:
                        await self.process(jid,row,config)
                except CloudError as exc:
                    if exc.code in ('LOGIN_REQUIRED','LOGIN_INVALID'):
                        raise
                    self.engine.update_item(row['id'],state='review',step='本文件同步未完成',message=str(exc))
                except UncertainState as exc:
                    self.engine.update_item(row['id'],state='review',step='本文件同步未完成',message=str(exc))
                await self.backfill_sidecars(jid, row, config)
        async with self.engine.publication_gate:
            await self.download_metadata(jid, config)
        self.engine.boundary(jid)
        # Each source is verified immediately before its output is published.
        # Only missing registered outputs need a live-namespace check in cleanup.
        warnings = []
        for row in self.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)):
            warnings.extend(json.loads(row['plan']).get('warnings',[]))
        reviews = self.store.db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='review'", (jid,)).fetchone()[0]
        if reviews:
            warnings.append(f'{reviews} 个文件同步未完成，保留旧输出；可重试未完成文件。')
        removed = 0
        if not warnings and not cached and config.get('cleanup_missing',True):
            async with self.engine.publication_gate:
                async with self.engine.cleanup_gate:
                    saved = json.loads(self.engine.job(jid)['config'])
                    revision = self.store.meta('sync_revision:'+self.engine.index.account(config))
                    if 'manifest_revision' not in saved or saved['manifest_revision'] != revision:
                        # 账号级 revision 只要有任何远端改动（转存、整理、改别的目录）就会变
                        # ——invalidate() 的注释就是「possible remote mutation」。以前据此直接
                        # 跳过清理，于是失效清理几乎永远不执行（现场 #92/#8/#3 都是这条）。
                        # cleanup() 自身已有三层保护：清单里有的跳过、扫描开始后才登记的跳过、
                        # live_file_exists 确认文件真没了才删；文件仍在但脱离范围会 raise
                        # UncertainState 中止清理。所以这里只留痕，不再阻止。
                        self.store.event('strm_sync',
                                         '扫描期间网盘内容有变化，仍继续核对失效输出（逐条 live 确认）。')
                    started = saved.get('manifest_started_at')
                    if not isinstance(started, (int, float)) or isinstance(started, bool) or started <= 0:
                        warnings.append('缺少本次扫描起始时间，已跳过失效清理；请重新同步。')
                    else:
                        cleanup_removed, cleanup_warnings = await self.cleanup(jid,config,after=started)
                        removed += cleanup_removed; warnings.extend(cleanup_warnings)
        if json.loads(self.engine.job(jid)['config']).get('output_changed') and config.get('notify_emby',True):
            await self.engine.notify_emby(jid)
        self.engine.boundary(jid)
        partial = bool(warnings) or self.engine.job(jid)['notify_state'] in ('failed','unknown')
        if not partial and self.store.get('sync_full_due'):
            self.store.set_many({'sync_full_due': 0})
        self.engine.update_job(jid,state='partial' if partial else 'completed',stage='STRM 同步结束',
            message=('复用近期已核对清单；本次未扫描全部云端目录，也不执行失效清理。' if cached else f'完整清单已核实；清理失效受管文件 {removed} 个。')+('；'.join(warnings) if warnings else '云端文件未修改。')+(f' 按生成规则跳过 {config.get("sync_filter_count",0)} 个文件'+('（'+'、'.join('%s：%s' % (item['name'], item.get('reason') or '不符合规则') for item in (config.get('sync_filter_names') or [])[:3])+'）' if config.get('sync_filter_names') else '')+'。' if config.get('sync_filter_count') else ''))


    async def download_metadata(self, jid, config):
        copied, skipped, failed, existing = 0, 0, 0, 0
        per_root = {}
        for source in config.get('sync_metadata', []):
            self.engine.boundary(jid)
            root=str(source.get('_root') or '')
            stat=per_root.setdefault(root,{'downloaded':0,'skipped':0,'failed':0,'existing':0,'seconds':0.0,'types':{}})
            relative = str(Path(self.relative(source,config)).parent / component(source['name']))
            path = self.engine.root / relative
            if path.exists():
                # 本地已有同名附属文件：这是「保留已有」，不是失败也不是规则跳过。
                existing += 1
                stat['existing'] += 1
                continue
            if (source.get('size') or 0) > 20*1024*1024:
                self.store.event('sync', '附属文件超过20MB下载上限，已跳过：'+source['name'])
                skipped += 1
                stat['skipped'] += 1
                continue
            started=time.monotonic()
            # 与字幕同一条契约：清单新鲜且带 sha1/size/pickcode 时不再逐条回查云端，
            # 下载后用本地摘要比对清单 sha1（正常路径零回查，只有对不上才核实一次）。
            fresh = bool(source.get('_scan_at')) and time.time()-source['_scan_at'] < 900
            cached_id = fresh and self.engine.measurements.fingerprint(source) and isinstance(source.get('pickcode'), str) and source['pickcode']
            try:
                if cached_id:
                    current = {'id':source['id'],'name':source['name'],'parent_id':source['parent_id'],
                               'size':source['size'],'sha1':source['sha1'],'pickcode':source['pickcode']}
                else:
                    current = await self.cloud.file(source['id'])
                    self.engine.check_source(source,current,source['name'],source['parent_id'])
                url, headers = await self.cloud.url(current['pickcode'],'StrimKeep/metadata')
                data = await self.engine.download(url,{**headers,'User-Agent':'StrimKeep/metadata'},20*1024*1024)
                if cached_id:
                    if (len(data)!=current['size'] or
                            hashlib.sha1(data).hexdigest().upper()!=current['sha1'].upper()):
                        self.engine.check_source(source,await self.cloud.file(source['id']),source['name'],source['parent_id'])
                        self.store.event('sync','附属文件内容与清单摘要不一致，已按云端现状写入：'+source['name'])
                else:
                    self.engine.check_source(source,await self.cloud.file(source['id']),source['name'],source['parent_id'])
                written=bool(write_new(path,data,self.engine.root))
                copied += written
                stat['downloaded'] += written
                if written:
                    _suffix=Path(source['name']).suffix.lower()
                    if _suffix=='.nfo':_type='nfo'
                    elif _suffix=='.json':_type='json'
                    elif _suffix in ('.jpg','.jpeg','.png','.webp'):_type='image'
                    elif _suffix in ('.ass','.srt','.ssa','.sub','.sup','.vtt','.idx'):_type='subtitle'
                    else:_type='other'
                    stat['types'][_type]=stat['types'].get(_type,0)+1
                if not written:stat['skipped']+=1
            except (ValueError,httpx.HTTPError,CloudError) as exc:
                if isinstance(exc,CloudError) and exc.code in ('LOGIN_REQUIRED','LOGIN_INVALID'):raise
                self.store.event('sync','附属文件下载未完成，保留已有文件：'+source['name'])
                failed += 1
                stat['failed'] += 1
            finally:
                stat['seconds']+=time.monotonic()-started
        current=json.loads(self.engine.job(jid)['config']);current['sync_metadata_stats']=per_root
        self.engine.update_job(jid,config=json.dumps(current,ensure_ascii=False))
        if copied or existing or skipped or failed:
            self.store.event('sync', f'附属文件新增 {copied} 个，已存在保留 {existing} 个，'
                            f'超限或未写入跳过 {skipped} 个，失败 {failed} 个。')
