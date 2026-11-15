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
from .message_templates import sync_task_context
from .local_scan import scan, identity_of_file


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
        """本地即事实：磁盘上有、内容对，才算「没变」。

        不再把台账里的「已同步」当作免检凭证 —— 用户删掉本地输出后，
        下一轮必须重新生成，而不是被一条旧记录挡下来。
        """
        account = self.engine.index.account(config)
        if account+':'+source['id'] in json.loads(self.store.meta('library_name_pending') or '{}'): return None
        if source.get('subtitle_warning'): return None
        relative = self.relative(source, config)
        local = self.engine.outputs.path(relative)
        if not local.is_file(): return None
        try:
            current = local.read_bytes()
        except OSError:
            return None
        old = self.engine.outputs.get(account, source['id'])
        if not old or not old['active']: return None
        if old['relative_path'] != relative: return None
        mapping = self.store.db.execute('SELECT * FROM playbacks WHERE id=?',(old['playback_id'],)).fetchone()
        if not mapping or mapping['revoked'] or mapping['file_id'] != source['id']: return None
        if identity_of(source) is not None:
            # 身份齐全：只认本轮清单能推出来的 play115 地址。
            if current not in play115_candidates(config['playback_base'], source, source['name']): return None
        else:
            # 身份不全的老条目只能写令牌地址，沿用历史形态，不改写已有输出。
            token = self.store.crypto.decrypt(old['token_cipher'].encode()).decode()
            forms = candidates(config['playback_base'], old['playback_id'], source['name'], token)
            forms += candidates(config['playback_base'], old['playback_id'],
                                Path(old['relative_path']).stem, token)
            if current not in forms: return None
        trail = [x['id'] for x in source['_path']]
        if json.loads(old['trail']) != trail:
            # 台账的目录链写旧了只补记录，不影响「要不要重写文件」。
            with self.store.db:
                self.store.db.execute('UPDATE output_registry SET trail=? WHERE account_id=? AND file_id=?',
                                      (json.dumps(trail, ensure_ascii=False, separators=(',', ':')), account, source['id']))
        # 字幕按内容核对：本地被删、或被换成别的文件，都要重建（不靠台账记忆）。
        seen_subtitles = []
        for sub in source.get('subtitles', []):
            suffix = subtitle_suffix(source['name'], sub['name'])
            path = self.engine.outputs.path(str(Path(relative).with_suffix('')) + suffix)
            if not path.is_file(): return None
            try:
                data = path.read_bytes()
            except OSError:
                return None
            if (len(data) != sub.get('size')
                    or hashlib.sha1(data).hexdigest().upper() != str(sub.get('sha1') or '').upper()):
                return None
            seen_subtitles.append({k: sub[k] for k in ('id','name','parent_id','size','sha1') if k in sub})
        previous_source = json.loads(old['source']) if old['source'] else {}
        if previous_source.get('subtitles') != seen_subtitles:
            with self.store.db:
                self.store.db.execute('UPDATE output_registry SET source=? WHERE account_id=? AND file_id=?',
                                      (json.dumps({**previous_source, 'subtitles': seen_subtitles},
                                                  ensure_ascii=False, separators=(',', ':')), account, source['id']))
        return {'relative_path':relative, 'target_name':source['name'], 'playback_id':old['playback_id'],
                'token_cipher':old['token_cipher'], 'local_files':json.loads(old['files']),
                'warnings':[], 'sync_unchanged':True}

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
        ids.index(source['_root'])  # selected root must actually be in the trail
        # 按 115 层级镜像：去掉最上面的网盘根，下面的目录名原样保留；
        # 显式设置了库根时用它替换第一层（归档库的既有语义）。
        parts = [component(entry['name'], 100) for entry in path[1:]]
        explicit = str(config.get('library_dir_name') or '')
        archive = str(config.get('archive_cid') or '')
        # 归档库就在第一层时，显式库根替换这一层（既有语义）；
        # 嵌在别的目录下面的库按真实层级镜像，不额外改名。
        if explicit and parts and (not archive or str(path[1]['id']) == archive):
            parts[0] = component(explicit, 100)
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
        """把一份云端文件落成本地 STRM（+ 字幕）。

        不再写任何「台账」：判断只看本轮清单与本地文件，输出内容自带身份，
        下一轮从文件本身就能重新认出来。
        """
        from .core import atomic_write, dumps, UncertainState
        source = json.loads(row['original'])
        plan = json.loads(row['plan'] or '{}')
        if not plan.get('relative_path'):
            plan = {'relative_path': self.relative(source, config), 'target_name': source['name'],
                    'local_files': {}, 'warnings': []}
            self.engine.update_item(row['id'], plan=plan, state='planned', step='同步计划已持久化')
        # 清单即事实：本轮扫描刚拿到的 id/name/size/sha1/pickcode 够新、目录链自洽，
        # 就不再逐文件回查云端（每文件两跳网络正是旧实现慢的主因）。
        trusted = False
        if config.get('trust_scan', True) and source.get('_scan_at'):
            stamp = source.get('_scan_at')
            trail = source.get('_path') or []
            age = time.time() - stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else -1
            trusted = bool(0 <= age < 900 and identity_of(source)
                           and self.engine.measurements.fingerprint(source)
                           and trail and trail[-1].get('id') == source.get('parent_id')
                           and source.get('_root') in [node.get('id') for node in trail])
        if trusted:
            actual = {'id': source['id'], 'name': source['name'], 'parent_id': source['parent_id'],
                      'size': source.get('size'), 'sha1': source.get('sha1', ''),
                      'pickcode': source.get('pickcode', '')}
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
            plan['relative_path'] = self.relative(source, config)
        identity = identity_of(actual) or identity_of(source)
        if identity is None:
            # 身份不全就只能回查一次；还缺就明确停下来，不写半成品地址。
            actual = await self.cloud.file(source['id'])
            self.engine.check_source(source, actual, source['name'], source['parent_id'])
            identity = identity_of(actual)
        if identity is None:
            self.engine.update_item(row['id'], plan=plan, state='review', step='缺少播放身份',
                                    message='文件缺少 pickcode / sha1 / 大小，无法生成播放地址。')
            return
        url = play115_url(config['playback_base'], *identity,
                          actual.get('name') or source['name']).encode()
        content = {plan['relative_path']: url}
        warnings = []
        subtitle_cache = []
        if source.get('subtitle_warning'):
            warnings.append('同主体多个视频的字幕归属不明，已保留原文件。')

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
            local = self.engine.outputs.path(relative)
            target_size, target_sha1 = sub.get('size'), str(sub.get('sha1') or '').upper()
            data = None
            # 本地已有且与清单摘要一致 → 直接用，不下载、不查库。
            if local.is_file() and local.stat().st_size <= 20 * 1024 * 1024:
                cached = local.read_bytes()
                if target_sha1 and len(cached) == target_size and hashlib.sha1(cached).hexdigest().upper() == target_sha1:
                    data = cached
            try:
                if data is None:
                    fresh = bool(sub.get('_scan_at')) and time.time() - sub['_scan_at'] < 900
                    pickcode = sub.get('pickcode') if fresh else ''
                    if not pickcode:
                        checked = await self.cloud.file(sub['id'])
                        self.engine.check_source(sub, checked, sub['name'], sub['parent_id'])
                        pickcode = checked.get('pickcode') or ''
                    link, headers = await self.cloud.url(pickcode, 'StrimKeep/0.5 Subtitle')
                    data = await self.engine.download(link, {**headers, 'User-Agent': 'StrimKeep/0.5 Subtitle'},
                                                      20 * 1024 * 1024)
                    if target_sha1 and (len(data) != target_size
                                        or hashlib.sha1(data).hexdigest().upper() != target_sha1):
                        self.store.event('sync', '字幕内容与清单摘要不一致，已按云端现状写入：' + sub['name'])
                subtitle_cache.append({k: sub[k] for k in ('id','name','parent_id','size','sha1') if k in sub})
                content[relative] = data
            except (httpx.HTTPError, ValueError, CloudError) as exc:
                if isinstance(exc, CloudError) and exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    raise
                warnings.append('外挂字幕下载失败，本文件下次同步会重试。')
        if not trusted:
            self.engine.check_source(source, await self.cloud.file(source['id']),
                                     source['name'], source['parent_id'])
        for relative, data in content.items():
            self.engine.boundary(jid)
            target = self.engine.outputs.path(relative)
            checksum = hashlib.sha256(data).hexdigest()
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                plan['local_files'][relative] = checksum
                self.mark_changed(jid)
                plan['output_changed'] = True
                atomic_write(target, data, self.engine.root)
            plan['local_files'][relative] = checksum
        plan['warnings'] = warnings
        with self.store.db:
            # 整理登记只更新路径（按文件身份，不再按播放身份）。
            self.store.db.execute('UPDATE organized_files SET relative_path=? WHERE file_id=?',
                                  (plan['relative_path'], source['id']))
            managed = self.store.db.execute(
                'SELECT i.id, i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=?',
                (source['id'],)).fetchone()
            if managed and managed['plan']:
                saved = json.loads(managed['plan'])
                ids = [x['id'] for x in source['_path']]
                if config.get('archive_cid') in ids:
                    saved['parts'] = [x['name'] for x in source['_path'][ids.index(config['archive_cid'])+1:]]
                saved.pop('strm_filtered', None)
                saved.update(relative_path=plan['relative_path'], local_files=plan['local_files'],
                             playback_base=config['playback_base'], target_name=source['name'],
                             dest=source['parent_id'])
                self.store.jobs.set_item_plan(managed['id'], saved)
        self.update_item(row['id'], plan=plan, state='done',
                         step='STRM 与字幕已核实同步', message='；'.join(warnings))

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
        from .media_info_policy import media_info_enabled
        missing_media = media_info_enabled(self.store) and settings.get('emby_media_info') and not target.with_name(target.stem+'-media-info.json').exists()
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

    # ---- 条目写库批量提交 ----------------------------------------------
    def begin_batch(self):
        self._batch = []

    def flush_batch(self):
        pending, self._batch = getattr(self, '_batch', None), None
        if not pending:
            return
        with self.store.db:
            for item_id, values in pending:
                self.engine.jobs.update_item(item_id, **values)

    def update_item(self, item_id, **values):
        pending = getattr(self, '_batch', None)
        if pending is None:
            self.engine.jobs.update_item(item_id, **values)
            return
        pending.append((item_id, values))
        if len(pending) >= 64:
            self.flush_batch()
            self._batch = []

    def present_ids(self, jid, config):
        """本轮清单里出现过的文件身份（用来判断「本地这份还算不算在范围内」）。"""
        present = {str(row['file_id']) for row in self.store.db.execute(
            'SELECT file_id FROM organize_items WHERE job_id=?', (jid,))}
        present |= {str(x) for x in config.get('sync_filtered_ids', [])}
        return present

    async def stale_check(self, jid, config):
        """失效差集：本地有、清单没有、云端也确实没了 → 只标记不删除。"""
        if not config.get('stale_detect'):
            return []
        account = self.engine.index.account(config)
        present = self.present_ids(jid, config)
        missing = []
        for relative, identity in scan(self.engine.root).items():
            if identity.get('kind') != 'netdisk':
                continue
            fid = str(identity['file_id'])
            if fid in present:
                continue
            if await self.cloud.live_file_exists(fid):
                continue
            missing.append({'account_id': account, 'file_id': fid, 'relative_path': relative})
        previous = self.store.get('stale_strms') or '[]'
        current = json.dumps(missing, ensure_ascii=False)
        if current != previous:
            self.store.set_many({'stale_strms': current})
            if missing:
                self.store.event('strm_sync', '失效 STRM 检测：发现 %d 条本地 strm 的网盘源文件已消失（只标记未删除）。'
                                 % len(missing))
        return missing

    async def stale_cleanup(self):
        """删除失效 STRM：只删「身份写在文件里、云端确实没了」的那一份。"""
        stale = json.loads(self.store.get('stale_strms') or '[]')
        account = self.engine.index.account({})
        cleaned, rest = [], []
        for item in stale:
            relative, fid = item.get('relative_path', ''), str(item.get('file_id', ''))
            try:
                if not fid or (item.get('account_id') and item['account_id'] != account):
                    rest.append({**item, 'error': '账号或文件身份不匹配，已保留。'}); continue
                local = self.engine.outputs.path(relative)
                identity, _ = identity_of_file(local)
                if not identity or identity.get('kind') != 'netdisk' or str(identity['file_id']) != fid:
                    rest.append({**item, 'error': '本地文件身份对不上，已保留。'}); continue
                if await self.cloud.live_file_exists(fid):
                    rest.append({**item, 'error': '网盘源文件仍存在，已保留。'}); continue
                from .internal_delete import remember
                if not remember(self.store, [relative]):
                    raise RuntimeError('内部删除登记失败，已保留本地文件。')
                local.unlink()
                cleaned.append(relative)
            except Exception as exc:
                rest.append({**item, 'error': str(exc)[:80]})
        self.store.set_many({'stale_strms': json.dumps(rest, ensure_ascii=False)})
        return cleaned, rest

    async def cleanup(self, jid, config, after=None):
        """清理：本地 STRM 的身份不在本轮清单里、云端也没了 → 删掉。"""
        if not config.get('cleanup_missing'):
            return 0, []
        present = self.present_ids(jid, config)
        removed, warnings = 0, []
        for relative, identity in scan(self.engine.root).items():
            if identity.get('kind') != 'netdisk':
                continue
            fid = str(identity['file_id'])
            if fid in present:
                continue
            self.engine.boundary(jid)
            try:
                if await self.cloud.live_file_exists(fid):
                    continue
                local = self.engine.outputs.path(relative)
                current, _ = identity_of_file(local)
                if not current or str(current.get('file_id')) != fid:
                    warnings.append('已保留身份变化的本地文件：' + relative); continue
                from .internal_delete import remember
                if not remember(self.store, [relative]):
                    warnings.append('内部删除登记失败，已保留：' + relative); continue
                local.unlink()
                removed += 1
                self.mark_changed(jid)
            except Exception as exc:
                warnings.append('清理失败（%s）：%s' % (relative, str(exc)[:60]))
        state = json.loads(self.engine.job(jid)['config']); state['sync_removed'] = removed
        self.engine.update_job(jid, config=json.dumps(state, ensure_ascii=False))
        return removed, warnings

    async def run(self, jid, config):
        if hasattr(self.cloud, 'use_scope'):
            self.cloud.use_scope(self.engine.current_account(), jid)
        if hasattr(self.cloud, 'reset_ledger'):
            self.cloud.reset_ledger()
        started = time.time()
        try:
            await self._run(jid, config)
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

    async def _run(self, jid, config):
        from .core import UncertainState
        self.engine.boundary(jid)
        job = self.engine.job(jid)
        if not job['manifest']:
            labels=config.get('directory_labels') or {}
            roots=config.get('source_cids') or []
            names = [str(labels.get(cid) or cid)[:80] for cid in roots[:5]]
            suffix = f' 等 {len(roots)} 个目录' if len(roots) > 5 else ''
            await self.engine.notifications.publish(f'strm:{jid}:start','📋 STRM 同步开始',
                sync_task_context(config)+'目录：'+'、'.join(names)+suffix,
                job_id=jid,category='strm_summary')
            # 手动和定时同步都必须发现网盘新增、改名与移动；仅在整理后
            # 的目标目录增量任务缩小范围，普通同步不使用旧快照或本地巡检。
            if self.engine.scan_receive_gate.locked():
                self.engine.update_job(jid, stage='等待目录扫描或转存核对')
            async with self.engine.scan_receive_gate:
                await self.manifest(jid, config)
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
        # Prefer existing cloud sidecars before generating missing metadata locally.
        async with self.engine.publication_gate:
            await self.download_metadata(jid, config)
        next_progress = 0.0
        self.begin_batch()
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
                    self.update_item(row['id'],plan=plan,state='done',step='输出未变更，已复用核对结果')
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
                    self.update_item(row['id'],state='review',step='本文件同步未完成',message=str(exc))
                except UncertainState as exc:
                    self.update_item(row['id'],state='review',step='本文件同步未完成',message=str(exc))
                await self.backfill_sidecars(jid, row, config)
        self.flush_batch()
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
