"""Read-only cloud synchronization; cleanup requires a complete, rechecked manifest."""
import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
from .cloud115 import CloudError, FATAL_CODES
from .media import SUBTITLES, VIDEOS, component, bind_subtitles, subtitle_suffix
from .strm_options import StrmOptions, skip_reason, sync_path_keyword, sync_path_skip_reason, write_new
from .organize_options import library_root
from .playaddr import candidates, identity_of, play115_candidates, play115_url, play_url
from .store import digest
from .sync_hash_worker import HashWorker, HashWorkerError
from .message_templates import sync_task_context
from .local_scan import fid_of, scan, identity_of_file


def stale_records(store):
    """Both the UI and the sync worker read the same persisted result."""
    value = store.get('stale_strms') or []
    return json.loads(value) if isinstance(value, str) else value


def stale_path_key(relative):
    return str(relative).replace('\\', '/')


@contextmanager
def measure(timings, stage):
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0.0) + (time.perf_counter() - start)


@asynccontextmanager
async def timed_gate(lock, timings, stage):
    with measure(timings, stage):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


class Sync:
    FULL_SCAN_SECONDS = 1800
    MAX_CLOUD_PROBES = 300
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
        if config.get('sync_trigger') == 'transfer':
            allowed = self.engine.tasks.transfer_sync_roots(config['sync_profile_id'])
            if any(not allowed.intersection(part['id'] for part in trail) for trail in paths.values()):
                raise UncertainState('转存目录已不在启用变更补扫的同步范围内，未发布同步清单。')
        if config.get('sync_trigger') in ('directory_change', 'cloud_activity'):
            profile = next((p for p in self.store.get('sync_profiles', [])
                            if p['id'] == config.get('sync_profile_id')), {})
            if config['sync_trigger'] == 'cloud_activity' and not profile.get('watch_changes'):
                raise UncertainState('该计划已关闭 115 变更同步，未发布同步清单。')
            allowed = set(profile.get('source_cids') or [profile.get('source_cid')])
            if any(not allowed.intersection(str(part['id']) for part in trail) for trail in paths.values()):
                raise UncertainState('所选目录已不在同步计划的范围内，未发布同步清单。')
        # Pick the outermost selected root, independent of selection order.
        effective = [cid for cid in roots if not any(other != cid and other in [x['id'] for x in paths[cid]] for other in roots)]
        batched = getattr(self.cloud, 'download_tree', None)
        fast = getattr(self.cloud, 'recursive_files_verified', None)
        completed_fast = False
        listing = 'directory'
        received_ids = set(config.get('sync_received_ids') or [])
        targeted = (config.get('sync_trigger') == 'transfer' and len(effective) == 1 and received_ids
                    and not (set(config.get('metadata_extensions') or []) - SUBTITLES))
        if targeted:
            root, = effective
            read = getattr(self.cloud, 'children_verified', None) or self.cloud.children
            started = time.monotonic()
            entries = await read(root)
            path = paths[root]
            root_files = [entry for entry in entries if not entry['is_dir']]
            if received_ids <= {str(entry['id']) for entry in root_files}:
                stamp = time.time()
                files = {entry['id']: {**entry, '_path': path, '_root': root, '_scan_at': stamp}
                         for entry in root_files if str(entry['id']) in received_ids
                         or Path(entry['name']).suffix.lower() in SUBTITLES}
                local = self.relative({'_path': path, '_root': root, 'name': 'scope.strm'}, config)
                seen, listing, completed_fast = {root}, 'receipt', True
                stats = {root: {'id': root, 'name': path[-1]['name'], 'directories': 1,
                                'files': len(root_files), 'local_path': str(self.engine.root / Path(local).parent),
                                'scan_seconds': time.monotonic() - started}}
        if not completed_fast and (callable(batched) or callable(fast)):
            try:
                if callable(batched):
                    seen, files, stats = await self.download_manifest(jid, config, effective, paths)
                    listing = 'bulk'
                    self.store.event('sync', '已通过递归目录、文件及批量详情完成清单。')
                else:
                    seen, files, stats = await self.recursive_manifest(jid, config, effective, paths)
                    listing = 'recursive'
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
        metadata_extensions = set(rules['metadata_extensions'])
        video_extensions = VIDEOS | set(rules['video_extensions'])
        subtitle_extensions = SUBTITLES & metadata_extensions
        selected, sidecars, subtitles, filtered, examples = [], [], [], [], []
        skipped_count = excluded_count = 0
        keywords = config.get('sync_exclude_keywords') or []
        for source in files.values():
            suffix = Path(source['name']).suffix.lower()
            is_sidecar = suffix in metadata_extensions
            reason = '' if is_sidecar else skip_reason(source, rules)
            keyword = sync_path_keyword(source, keywords) if is_sidecar or not reason else ''
            if not reason and not keyword and not is_sidecar:
                selected.append(source)
                continue
            # All entries were present in the verified listing, including excluded paths.
            filtered.append(source['id'])
            if keyword:
                excluded_count += 1
                if len(examples) < 10:
                    examples.append({'name': source['name'], 'reason': sync_path_skip_reason(keyword)})
            elif is_sidecar:
                sidecars.append(source)
                if suffix in subtitle_extensions:
                    subtitles.append(source)
            elif suffix in video_extensions:
                skipped_count += 1
                if len(examples) < 10:
                    examples.append({'name': source['name'], 'reason': reason})
        videos = bind_subtitles(selected + subtitles, rules['video_extensions'], subtitle_extensions)
        bound_subtitles = {sub['id'] for video in videos for sub in video.get('subtitles', [])}
        manifest_details = {
            'sync_filtered_ids': filtered,
            'sync_metadata': [] if listing == 'receipt' else
                [source for source in sidecars if source['id'] not in bound_subtitles],
            'sync_filter_count': skipped_count,
            'sync_path_excluded_count': excluded_count,
            'sync_filter_names': examples,
        }
        config.update(manifest_details)
        self.store.jobs.replace_manifest(jid, [(v['id'], dumps(v)) for v in videos])
        current = json.loads(self.engine.job(jid)['config'])
        current.update(manifest_details)
        current['sync_mode'] = 'receipt' if listing == 'receipt' else 'full'
        current['sync_directory_stats']=stats
        current['sync_listing']=listing
        current['sync_effective_roots']=effective
        current['manifest_at']=time.time()
        current['manifest_started_at']=manifest_started_at
        current['manifest_revision']=revision_at_start
        self.engine.update_job(jid,config=dumps(current))

    def relative(self, source, config):
        path = source['_path']; ids = [x['id'] for x in path]
        ids.index(source['_root'])  # selected root must actually be in the trail
        # Keep the actual cloud path; never inject names or rewrite directory components.
        parts = [entry['name'] for entry in path if str(entry['id']) != '0']
        # Never silently turn separators into new path components.
        for name in parts + [source['name']]:
            if name in ('.','..') or '/' in name or '\\' in name or '\0' in name: raise ValueError('同步文件名含不安全路径字符。')
        return str(Path(*parts, Path(source['name']).stem + '.strm'))

    def mark_changed(self, jid):
        from .core import dumps
        config = json.loads(self.engine.job(jid)['config'])
        if config.get('output_changed'):
            return
        config['output_changed'] = True
        self.engine.update_job(jid, config=dumps(config))

    async def process(self, jid, row, config, timings):
        """把一份云端文件落成本地 STRM（+ 字幕）。

        同步以本轮清单和本地文件为准，来源证据用于后续归属核验。
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
        if source.get('_scan_at'):
            stamp = source.get('_scan_at')
            trail = source.get('_path') or []
            age = time.time() - stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else -1
            trusted = bool(0 <= age < 900 and identity_of(source)
                           and self.engine.measurements.fingerprint(source)
                           and trail and trail[-1].get('id') == source.get('parent_id')
                           and source.get('_root') in [node.get('id') for node in trail])
        with measure(timings, 'verify'):
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

        with measure(timings, 'sidecars'):
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
                        fresh = (bool(sub.get('_scan_at')) and time.time() - sub['_scan_at'] < 900
                                 and identity_of(sub) is not None)
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
            with measure(timings, 'verify'):
                self.engine.check_source(source, await self.cloud.file(source['id']),
                                         source['name'], source['parent_id'])
        for relative, data in content.items():
            with measure(timings, 'verify'):
                self.engine.boundary(jid)
                target = self.engine.outputs.path(relative)
                checksum = hashlib.sha256(data).hexdigest()
                if relative.lower().endswith('.strm'):
                    try:
                        self.engine.outputs.guard(self.engine.index.account(config), source['id'], relative,
                                                  {relative: checksum})
                    except UncertainState as exc:
                        self.update_item(row['id'], plan=plan, state='review', step='同名文件已存在',
                                         message=str(exc))
                        return
                if (not relative.lower().endswith('.strm') and target.exists()
                        and (target.is_symlink() or not target.is_file()
                             or hashlib.sha256(target.read_bytes()).hexdigest() != checksum)):
                    old = self.engine.outputs.get(self.engine.index.account(config), source['id'])
                    try:
                        recorded = json.loads(old['files'] or '{}').get(relative) if old else None
                    except (ValueError, TypeError, AttributeError):
                        recorded = None
                    owner = self.store.db.execute(
                        'SELECT account_id,file_id,digest FROM owned_outputs WHERE relative_path=?',
                        (relative,)).fetchone()
                    if (target.is_symlink() or not target.is_file() or not recorded or not owner
                            or (owner['account_id'], str(owner['file_id']), owner['digest'])
                            != (str(self.engine.index.account(config)), str(source['id']), recorded)
                            or hashlib.sha256(target.read_bytes()).hexdigest() != recorded):
                        warnings.append('本地字幕已修改或归属不明，已保留：' + relative)
                        plan['local_files'].pop(relative, None)
                        continue
                should_write = not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != checksum
            if should_write:
                existed = target.is_file()
                plan['local_files'][relative] = checksum
                self.mark_changed(jid)
                plan['output_changed'] = True
                # 通知要能说清「新增了几条 STRM、更新了几条、附属文件写了几个」。
                if relative.lower().endswith('.strm'):
                    plan['sync_created' if not existed else 'sync_updated'] = True
                else:
                    plan['sync_attachment_created' if not existed else 'sync_attachment_updated'] = True
                with measure(timings, 'publish'):
                    atomic_write(target, data, self.engine.root)
                    # 刚落盘的文件要立刻能被「按 fid 找本地文件」看到（只更新这一条）。
                    self.engine.outputs.note(self.engine.index.account(config), relative, target,
                                             identity_of_file(target)[0])
            plan['local_files'][relative] = checksum
        account = self.engine.index.account(config)
        old = self.engine.outputs.get(account, source['id'])
        self.engine.outputs.remember(
            account, {**actual, 'subtitles': subtitle_cache},
            [entry['id'] for entry in source['_path']],
            {**plan, 'playback_id': old['playback_id'] if old else '',
             'token_cipher': old['token_cipher'] if old else ''})
        plan['warnings'] = warnings
        if not plan.get('sync_created') and not plan.get('sync_updated'):
            plan['sync_unchanged'] = True
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

    async def probe_technical_item(self, jid, row):
        from .fsutil import atomic_write, write_once
        from .media_info_policy import media_info_enabled
        from .native_media_info import NativeMediaInfo, sync_sidecar_identity

        plan = json.loads(row['plan'])
        target = self.engine.outputs.path(plan['relative_path'])
        sidecar = target.with_name(target.stem + '-media-info.json')
        source = json.loads(row['original'])
        if target.is_symlink() or sidecar.is_symlink():
            return 'preserved'
        output_identity, _ = identity_of_file(target)
        if not output_identity or output_identity.get('kind') != 'netdisk':
            raise ValueError('STRM 缺少可核实的网盘身份')
        if not self.engine.measurements.fingerprint(source) or not source.get('pickcode'):
            current = await self.cloud.file(source['id'])
            self.engine.check_source(source, current, source['name'], source['parent_id'])
            source = current
        if ((output_identity['file_id'], output_identity['sha1'], output_identity['size'], output_identity['pickcode'])
                != (str(source['id']), str(source.get('sha1') or '').upper(), source.get('size'), source.get('pickcode'))):
            raise ValueError('STRM 与云盘源文件身份不一致')
        identity = {key: output_identity[key] for key in ('file_id', 'sha1', 'size')}

        existing_bytes = None
        if sidecar.exists():
            try:
                existing_bytes = sidecar.read_bytes()
                existing = json.loads(existing_bytes)
            except (OSError, ValueError):
                return 'preserved'
            owner = sync_sidecar_identity(existing)
            if not owner:
                return 'preserved'
            streams = existing.get('streams')
            if owner == identity and isinstance(streams, list) and any(
                isinstance(stream, dict) and stream.get('Type') == 'Video' for stream in streams
            ):
                return 'reused'

        media, _, _ = await self.engine.measurements.measure(jid, source)
        item = NativeMediaInfo.measured_item(media, identity['size'], {})
        if not item:
            raise ValueError('未取得有效的视频流')
        content = (json.dumps({
            'version': 1, 'source': 'ffprobe', 'netdisk_source': identity,
            'size': item['Size'], 'duration_ticks': item['RunTimeTicks'], 'bitrate': item['Bitrate'],
            'streams': item['MediaStreams'], 'chapters': [],
        }, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        async with self.engine.publication_gate:
            self.engine.boundary(jid)
            if not media_info_enabled(self.store):
                return 'disabled'
            current_identity, _ = identity_of_file(target)
            if target.is_symlink() or current_identity != output_identity:
                raise ValueError('探测期间 STRM 身份已变化')
            if existing_bytes is None:
                return 'written' if write_once(sidecar, content, self.engine.root) else 'preserved'
            if sidecar.is_symlink() or not sidecar.is_file() or sidecar.read_bytes() != existing_bytes:
                return 'preserved'
            atomic_write(sidecar, content, self.engine.root)
        return 'written'

    async def extract_technical(self, jid):
        from .core import UncertainState, dumps
        from .media_info_policy import media_info_enabled

        rows = self.store.db.execute(
            "SELECT * FROM organize_items WHERE job_id=? AND state='done' ORDER BY id", (jid,)).fetchall()
        stats = dict.fromkeys(('written', 'reused', 'preserved', 'failed', 'pending'), 0)
        total = len(rows)
        next_progress = 0.0
        for position, row in enumerate(rows):
            if not media_info_enabled(self.store):
                stats['pending'] = total - position
                stats['disabled'] = True
                break
            self.engine.boundary(jid)
            now = time.monotonic()
            if now >= next_progress:
                self.engine.update_job(jid, stage=f'提取技术媒体信息 {position + 1}/{total}')
                next_progress = now + 1.0
            try:
                outcome = await self.probe_technical_item(jid, row)
            except (CloudError, UncertainState, OSError, ValueError, httpx.HTTPError) as exc:
                source = json.loads(row['original'])
                if isinstance(exc, CloudError):
                    reason = exc.code
                elif isinstance(exc, (UncertainState, ValueError)):
                    reason = str(exc)[:100]
                else:
                    reason = type(exc).__name__
                self.store.event('sync', f'任务 {jid}：媒体信息提取失败 · {source["name"]} · {reason}')
                stats['failed'] += 1
                if isinstance(exc, CloudError) and exc.code in FATAL_CODES:
                    stats['pending'] = total - position - 1
                    break
                continue
            if outcome == 'disabled':
                stats['pending'] = total - position
                stats['disabled'] = True
                break
            stats[outcome] += 1
        parts = [f'写入 {stats["written"]} 个', f'复用 {stats["reused"]} 个']
        for key, label in (('preserved', '保留原文件'), ('failed', '失败'), ('pending', '待提取')):
            if stats[key]:
                parts.append(f'{label} {stats[key]} 个')
        if stats.get('disabled'):
            parts.append('媒体信息总开关已关闭')
        stats['summary'] = '技术媒体信息：' + ' · '.join(parts)
        config = json.loads(self.engine.job(jid)['config'])
        config['sync_technical_stats'] = stats
        self.engine.update_job(jid, config=dumps(config))
        return stats

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

    async def tidy_moved(self, jid, config, local_files):
        """同一文件号出现在两个本地路径时，保留本轮清单算出来的那个，删掉另一个。"""
        expected = {}
        for row in self.store.db.execute('SELECT file_id, plan FROM organize_items WHERE job_id=?', (jid,)):
            plan_row = json.loads(row['plan'] or '{}')
            if plan_row.get('relative_path'):
                expected[str(row['file_id'])] = plan_row['relative_path']
        removed = set()
        for relative, identity in local_files.items():
            if identity.get('kind') != 'netdisk':
                continue
            fid = str(identity['file_id'])
            want = expected.get(fid)
            if not want or want == relative:
                continue
            if not self.engine.outputs.path(want).is_file():
                continue
            try:
                old_path = self.engine.outputs.path(relative)
                old_identity, _ = identity_of_file(old_path)
                new_identity, _ = identity_of_file(self.engine.outputs.path(want))
                if (old_path.is_symlink() or not old_identity or not new_identity
                        or old_identity.get('kind') != 'netdisk' or new_identity.get('kind') != 'netdisk'
                        or str(old_identity.get('file_id')) != fid or str(new_identity.get('file_id')) != fid):
                    continue
                old_path.unlink()
                with self.store.db:
                    self.store.db.execute(
                        'DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?',
                        (relative, str(self.engine.index.account(config)), fid))
                    self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)',
                                          (jid, 'removed', relative))
                removed.add(relative)
                self.mark_changed(jid)
            except Exception as exc:
                self.store.event('sync', '旧位置清理失败（%s）：%s' % (relative, str(exc)[:60]))
        if removed:
            self.engine.outputs.invalidate()
            self.forget_removed(config, local_files, removed)
        return len(removed)

    def scan_prefixes(self, jid, config):
        """Return only the complete local roots proven by this job's manifest."""
        saved = json.loads(self.engine.job(jid)['config'])
        stats = config.get('sync_directory_stats') or saved.get('sync_directory_stats') or {}
        roots = config.get('sync_effective_roots') or saved.get('sync_effective_roots') or config.get('source_cids') or []
        base = self.engine.root.resolve()
        prefixes = set()
        for cid in roots:
            local = str((stats.get(str(cid)) or {}).get('local_path') or '')
            if not local:
                continue
            try:
                relative = Path(local).resolve().relative_to(base)
            except (ValueError, OSError):
                continue
            if relative.parts:
                prefixes.add(str(relative))
            elif str(cid) == '0':
                # The user explicitly selected the 115 root.
                prefixes.add('.')
        return prefixes

    def scan_scope(self, jid, config):
        found = {}
        prefixes = {Path(prefix) for prefix in self.scan_prefixes(jid, config)}
        for prefix in sorted(prefixes):
            if any(parent in prefixes for parent in prefix.parents):
                continue
            base = self.engine.root / prefix
            if not base.is_dir():
                continue
            for relative, identity in scan(base).items():
                found[str(Path(prefix) / relative)] = identity
        return found

    def present_ids(self, jid, config):
        """本轮清单里出现过的文件身份（用来判断「本地这份还算不算在范围内」）。"""
        present = {str(row['file_id']) for row in self.store.db.execute(
            'SELECT file_id FROM organize_items WHERE job_id=?', (jid,))}
        present |= {str(x) for x in config.get('sync_filtered_ids', [])}
        return present

    def merge_stale_observations(self, account, observations):
        """Replace only paths actually verified by this run for this account."""
        if not observations:
            return False
        with self.store.transaction(immediate=True):
            previous = stale_records(self.store)
            pending = dict(observations)
            current = []
            for item in previous:
                key = stale_path_key(item.get('relative_path', ''))
                if str(item.get('account_id') or '') == str(account) and key in observations:
                    item = pending.pop(key, None)
                if item is not None:
                    current.append(item)
            current.extend(item for item in pending.values() if item is not None)
            if current != previous:
                self.store.set_many({'stale_strms': json.dumps(current, ensure_ascii=False)})
                return True
        return False

    def forget_removed(self, config, local_files, removed):
        if not removed:
            return
        for relative in removed:
            local_files.pop(relative, None)
        self.merge_stale_observations(self.engine.index.account(config),
                                      {stale_path_key(relative): None for relative in removed})

    @staticmethod
    def netdisk_digest(path, fid):
        verified, fingerprint = identity_of_file(path)
        if (not path.is_symlink() and verified and verified['kind'] == 'netdisk'
                and str(verified['file_id']) == fid):
            return fingerprint
        return None

    async def stale_check(self, jid, config, local_files):
        """失效差集：本地有、清单没有、云端也确实没了 → 只标记不删除。"""
        if not config.get('stale_detect'):
            return []
        account = self.engine.index.account(config)
        present = self.present_ids(jid, config)
        tracked = {stale_path_key(item.get('relative_path', '')) for item in stale_records(self.store)
                   if str(item.get('account_id') or '') == str(account)}
        observations = {}
        probes = 0
        capped = False
        for relative, identity in local_files.items():
            if identity.get('kind') != 'netdisk':
                continue
            fid = str(identity['file_id'])
            key = stale_path_key(relative)
            if fid in present:
                if key in tracked and self.netdisk_digest(self.engine.outputs.path(relative), fid):
                    observations[key] = None
                continue
            if probes >= self.MAX_CLOUD_PROBES:
                capped = True
                continue
            local = self.engine.outputs.path(relative)
            fingerprint = self.netdisk_digest(local, fid)
            if not fingerprint:
                continue
            self.engine.boundary(jid)
            probes += 1
            exists = await self.cloud.live_file_exists(fid)
            if self.netdisk_digest(local, fid) == fingerprint:
                observations[key] = None if exists else {'account_id': account, 'file_id': fid, 'relative_path': relative}
        if capped:
            self.store.event('strm_sync', '失效检测达到单轮上限（%d），已保留其余候选的原记录。' % self.MAX_CLOUD_PROBES)
        missing = [item for item in observations.values() if item is not None]
        if self.merge_stale_observations(account, observations) and missing:
            self.store.event('strm_sync', '失效 STRM 检测：本轮确认 %d 条本地 STRM 的网盘源文件已消失。' % len(missing))
        return missing

    async def stale_cleanup(self):
        """删除失效 STRM：只删「身份写在文件里、云端确实没了」的那一份。"""
        stale = stale_records(self.store)
        account = self.engine.index.account({})
        cleaned, results = [], {}

        def key(item):
            return (str(item.get('account_id') or ''), stale_path_key(item.get('relative_path', '')), str(item.get('file_id', '')))

        for item in stale:
            relative, fid = item.get('relative_path', ''), str(item.get('file_id', ''))
            error = ''
            try:
                if not fid or (item.get('account_id') and str(item['account_id']) != str(account)):
                    error = '账号或文件身份不匹配，已保留。'
                else:
                    local = self.engine.outputs.path(relative)
                    fingerprint = self.netdisk_digest(local, fid)
                    if not fingerprint:
                        error = '本地文件身份对不上，已保留。'
                    elif await self.cloud.live_file_exists(fid):
                        error = '网盘源文件仍存在，已保留。'
                    elif self.netdisk_digest(self.engine.outputs.path(relative), fid) != fingerprint:
                        error = '本地文件身份已变化，已保留。'
                    else:
                        local.unlink()
                        with self.store.db:
                            self.store.db.execute(
                                'DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?',
                                (relative, str(account), fid))
                        cleaned.append(relative)
            except Exception as exc:
                error = str(exc)[:80] or '清理未完成，已保留记录。'
            results[key(item)] = (item, {**item, 'error': error} if error else None)
        if cleaned:
            self.engine.outputs.invalidate()
        with self.store.transaction(immediate=True):
            latest = stale_records(self.store)
            rest = []
            for item in latest:
                before, after = results.get(key(item), (None, item))
                if before != item:
                    after = item
                if after is not None:
                    rest.append(after)
            if rest != latest:
                self.store.set_many({'stale_strms': json.dumps(rest, ensure_ascii=False)})
        return cleaned, rest

    async def cleanup(self, jid, config, local_files, *, after):
        """清理：本地 STRM 的身份不在本轮清单里、云端也没了 → 删掉。"""
        if config.get('cleanup_missing', True) is False:
            return 0, []
        present = self.present_ids(jid, config)
        account = str(self.engine.index.account(config))
        removed, warnings = set(), []
        probes = 0
        for relative, identity in local_files.items():
            if identity.get('kind') != 'netdisk':
                continue
            fid = str(identity['file_id'])
            if fid in present:
                continue
            if probes >= self.MAX_CLOUD_PROBES:
                warnings.append('清理达到单轮上限（%d 个候选），其余下次再看。' % self.MAX_CLOUD_PROBES)
                break
            self.engine.boundary(jid)
            try:
                local = self.engine.outputs.path(relative)
                fingerprint = self.netdisk_digest(local, fid)
                if not fingerprint:
                    warnings.append('已保留身份变化的本地文件：' + relative)
                    continue
                probes += 1
                if await self.cloud.live_file_exists(fid):
                    continue
                recent = self.store.db.execute(
                    'SELECT 1 FROM output_registry WHERE account_id=? AND file_id=? AND relative_path=? AND updated>? AND active=1',
                    (account, fid, relative, after)).fetchone()
                if recent or self.netdisk_digest(self.engine.outputs.path(relative), fid) != fingerprint:
                    warnings.append('已保留扫描期间更新的本地文件：' + relative)
                    continue
                local.unlink()
                removed.add(relative)
                with self.store.db:
                    self.store.db.execute(
                        'DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?',
                        (relative, account, fid))
                    self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)',
                                          (jid, 'removed', relative))
                self.mark_changed(jid)
            except Exception as exc:
                warnings.append('清理失败（%s）：%s' % (relative, str(exc)[:60]))
        self.forget_removed(config, local_files, removed)
        if removed:
            self.engine.outputs.invalidate()
        state = json.loads(self.engine.job(jid)['config']); state['sync_removed'] = len(removed)
        self.engine.update_job(jid, config=json.dumps(state, ensure_ascii=False))
        return len(removed), warnings

    async def run(self, jid, config):
        if hasattr(self.cloud, 'use_scope'):
            self.cloud.use_scope(self.engine.current_account(), jid)
        if hasattr(self.cloud, 'reset_ledger'):
            self.cloud.reset_ledger()
        started, clock = time.time(), time.perf_counter()
        job = self.engine.job(jid)
        saved = json.loads(job['config'])
        previous = saved.pop('sync_metrics', {})
        queue = previous.get('queue', max(0, (saved.get('sync_execution_started_at') or started) - job['created']))
        attempt = previous.get('attempt', 0) + 1
        timings = {}
        saved['sync_execution_started_at'] = started
        self.engine.update_job(jid, config=json.dumps(saved, ensure_ascii=False))
        try:
            with measure(timings, 'connection'):
                await self.cloud.status()
            await self._run(jid, config, timings)
            # 这个时间点只在本次真正跑完之后才更新，而且记的是**开始**时刻：
            # 扫描期间发生的改动下一轮还会被看到，不会漏。
            if not config.get('sync_trigger') and self.engine.job(jid)['state'] in ('completed', 'partial'):
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
            execution = time.perf_counter() - clock
            if hasattr(self.cloud, 'use_scope'):
                self.cloud.use_scope(self.engine.current_account(), jid)
            ledger = self.cloud.ledger_summary() if hasattr(self.cloud, 'ledger_summary') else None
            saved = json.loads(self.engine.job(jid)['config'])
            counts = self.store.db.execute(
                """SELECT COUNT(*), COALESCE(SUM(json_extract(plan,'$.sync_unchanged')=1),0),
                          COALESCE(SUM(json_extract(plan,'$.sync_created')=1),0),
                          COALESCE(SUM(json_extract(plan,'$.sync_updated')=1),0)
                   FROM organize_items WHERE job_id=?""", (jid,)).fetchone()
            saved['sync_metrics'] = {
                'queue': round(queue, 3), 'attempt': attempt,
                'mode': 'full' if config.get('_force_full') else 'incremental',
                'execution': round(execution, 3), 'total': round(max(0, time.time() - job['created']), 3),
                'stages': {key: round(value, 3) for key, value in timings.items()},
                'videos': counts[0], 'reused': counts[1], 'created': counts[2], 'updated': counts[3],
            }
            if 'scan' in timings and self.engine.job(jid)['manifest']:
                scopes = list((saved.get('sync_directory_stats') or {}).values())
                saved['sync_metrics']['scan'] = {
                    'listing': saved.get('sync_listing'),
                    'directories': sum(scope.get('directories', 0) for scope in scopes),
                    'directories_exact': all(scope.get('directories_exact', True) for scope in scopes),
                    'files': sum(scope.get('files', 0) for scope in scopes),
                }
            if ledger:
                saved['request_ledger'] = ledger
                saved['sync_metrics']['requests'] = {key: ledger[key] for key in ('read', 'write', 'blocked') if key in ledger}
                self.store.event('strm_sync', f'📊 同步请求账本：{ledger["text"]}')
            self.engine.update_job(jid, config=json.dumps(saved, ensure_ascii=False))
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

    async def _run(self, jid, config, timings):
        from .core import UncertainState
        self.engine.boundary(jid)
        job = self.engine.job(jid)
        if not job['manifest']:
            labels=config.get('directory_labels') or {}
            roots=config.get('source_cids') or []
            names = [str(labels.get(cid) or cid)[:80] for cid in roots[:5]]
            suffix = f' 等 {len(roots)} 个目录' if len(roots) > 5 else ''
            notice = asyncio.create_task(self.engine.notifications.publish(
                f'strm:{jid}:start','📋 STRM 同步开始',
                sync_task_context(config)+'目录：'+'、'.join(names)+suffix,
                job_id=jid,category='strm_summary'))
            self.engine.notice_tasks.add(notice)
            def notice_done(task):
                self.engine.notice_tasks.discard(task)
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    try:
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE notifications SET telegram_state='unknown',next_try=0 "
                                "WHERE event_key=? AND telegram_state='inflight'",
                                (f'strm:{jid}:start',))
                        self.store.event('sync', '同步开始通知发送异常，已交由通知重试。')
                    except Exception:
                        pass
            notice.add_done_callback(notice_done)
            # Let publish persist its outbox record before a result can refer
            # to it, without waiting for Telegram network delivery.
            await asyncio.sleep(0)
        else:
            # 续跑的清单可能已经是几小时前的：期间库里新增/移动过的文件不在其中，
            # 拿它去授权清理会被正确拦下（也等于永远做不完）。过期即重建。
            saved = json.loads(job['config'])
            stamp = saved.get('manifest_at') or job['updated']
            if config.get('sync_trigger') or saved.get('sync_mode') == 'cached' or time.time() - float(stamp or 0) > self.FULL_SCAN_SECONDS:
                self.engine.update_job(jid, manifest=0, stage='清单已过期，重新读取云端目录')
        if not self.engine.job(jid)['manifest']:
            if self.engine.scan_receive_gate.locked():
                self.engine.update_job(jid, stage='等待目录扫描或转存核对')
            async with timed_gate(self.engine.scan_receive_gate, timings, 'scan_wait'):
                with measure(timings, 'scan'):
                    await self.manifest(jid, config)
        saved_config = json.loads(self.engine.job(jid)['config'])
        config.update({k:saved_config[k] for k in ('sync_filtered_ids','sync_metadata','sync_filter_count',
                                                'sync_path_excluded_count','sync_filter_names',
                                                'sync_directory_stats','sync_effective_roots') if k in saved_config})
        # Network downloads run outside the publication gate; only the final
        # verified local write needs to exclude concurrent publishers.
        with measure(timings, 'sidecars'):
            await self.download_metadata(jid, config)
        timings.setdefault('verify', 0.0)
        timings.setdefault('publish', 0.0)
        next_progress = 0.0
        for row in self.store.db.execute("SELECT * FROM organize_items WHERE job_id=? AND state NOT IN ('done','skipped') ORDER BY id",(jid,)).fetchall():
            self.engine.boundary(jid)
            now = time.monotonic()
            if now >= next_progress:
                self.engine.update_job(jid,stage='同步：'+json.loads(row['original'])['name'])
                next_progress = now + 1.0
            async with timed_gate(self.engine.publication_gate, timings, 'publish_wait'):
                with measure(timings, 'verify'):
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
                with measure(timings, 'sidecars'):
                    await self.backfill_sidecars(jid, row, config)
                continue
            else:
                try:
                    async with timed_gate(self.engine.publication_gate, timings, 'publish_wait'):
                        await self.process(jid, row, config, timings)
                except CloudError as exc:
                    if exc.code in ('LOGIN_REQUIRED','LOGIN_INVALID'):
                        raise
                    self.update_item(row['id'],state='review',step='本文件同步未完成',message=str(exc))
                except UncertainState as exc:
                    self.update_item(row['id'],state='review',step='本文件同步未完成',message=str(exc))
                with measure(timings, 'sidecars'):
                    await self.backfill_sidecars(jid, row, config)
        # 发布时已逐项记录必要证据；结束时只使磁盘扫描缓存失效。
        try:
            await asyncio.to_thread(self.engine.outputs.reindex, self.engine.index.account(config))
        except Exception as exc:                    # 索引只是缓存，失败不影响本次同步
            self.store.event('sync', '本地索引重建未完成：%s' % str(exc)[:60])
        self.engine.boundary(jid)
        warnings = []
        local_files = {}
        if not config.get('sync_trigger') or config.get('stale_detect'):
            try:
                with measure(timings, 'local_scan'):
                    local_files = await asyncio.to_thread(self.scan_scope, jid, config)
            except OSError as exc:
                warnings.append('本地 STRM 扫描未完成：%s' % str(exc)[:60])
        if local_files and not config.get('sync_trigger'):
            try:
                # 旧位置的副本在新位置写好后收尾，与「清理缺失」开关无关。
                async with timed_gate(self.engine.publication_gate, timings, 'publish_wait'):
                    with measure(timings, 'local_cleanup'):
                        await self.tidy_moved(jid, config, local_files)
            except Exception as exc:
                self.store.event('sync', '旧副本清理跳过：%s' % str(exc)[:60])
        for row in self.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)):
            warnings.extend(json.loads(row['plan']).get('warnings',[]))
        reviews = self.store.db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='review'", (jid,)).fetchone()[0]
        if reviews:
            warnings.append(f'{reviews} 个文件同步未完成，保留旧输出；可重试未完成文件。')
        removed = 0
        if not warnings and not config.get('sync_trigger') and config.get('cleanup_missing',True):
            async with timed_gate(self.engine.publication_gate, timings, 'publish_wait'):
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
                        try:
                            with measure(timings, 'local_cleanup'):
                                cleanup_removed, cleanup_warnings = await self.cleanup(jid, config, local_files, after=started)
                        except Exception as exc:
                            cleanup_removed, cleanup_warnings = 0, ['失效清理未能完成：%s' % str(exc)[:60]]
                        removed += cleanup_removed; warnings.extend(cleanup_warnings)
        if config.get('stale_detect'):
            # The task is not complete until this configured check finishes.
            with measure(timings, 'local_cleanup'):
                await self.stale_check(jid, config, local_files)
        technical = {}
        if config.get('metadata_mode') == 'technical':
            with measure(timings, 'technical'):
                technical = await self.extract_technical(jid)
        if json.loads(self.engine.job(jid)['config']).get('output_changed') and config.get('notify_emby',True):
            with measure(timings, 'emby'):
                await self.engine.notify_emby(jid)
        self.engine.boundary(jid)
        partial = bool(warnings or technical.get('failed')) or self.engine.job(jid)['notify_state'] in ('failed','unknown')
        if technical:
            warnings.append(technical['summary'])
        if not partial and not config.get('sync_trigger') and self.store.get('sync_full_due'):
            self.store.set_many({'sync_full_due': 0})
        summary = f'完整清单已核实；清理失效受管文件 {removed} 个。'
        if config.get('sync_trigger') == 'transfer':
            summary = '已核实转存目录并补齐输出，失效清理交由常规同步。'
        elif config.get('sync_trigger') == 'cloud_activity':
            summary = '已核实 115 变更目录并补齐输出，失效清理交由常规同步。'
        messages = [summary, '；'.join(warnings) if warnings else '云端文件未修改。']
        if config.get('sync_filter_count'):
            messages.append(f'按生成规则跳过 {config["sync_filter_count"]} 个文件。')
        if config.get('sync_path_excluded_count'):
            messages.append(f'按路径排除词跳过 {config["sync_path_excluded_count"]} 个文件。')
        if config.get('sync_filter_names'):
            examples = [f'{item["name"]}（{item["reason"]}）' for item in config['sync_filter_names'][:3]]
            messages.append('、'.join(examples) + '。')
        self.engine.update_job(jid, state='partial' if partial else 'completed', stage='STRM 同步结束',
                               message=' '.join(messages))


    async def download_metadata(self, jid, config):
        from .core import UncertainState
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
            account = self.engine.index.account(config)
            revision = self.store.meta('sync_revision:' + account)
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
                async with self.engine.publication_gate:
                    self.engine.boundary(jid)
                    current_revision = self.store.meta('sync_revision:' + account)
                    if current_revision != revision:
                        checked = await self.cloud.file(source['id'])
                        self.engine.check_source(source, checked, source['name'], source['parent_id'])
                        trail = await self.cloud.path_info(source['parent_id'])
                        if trail != source.get('_path') or self.store.meta('sync_revision:' + account) != current_revision:
                            raise UncertainState('附属文件目录已变化，下次同步重新核对。')
                    write_task = asyncio.create_task(asyncio.to_thread(
                        write_new, path, data, self.engine.root))
                    try:
                        written = bool(await asyncio.shield(write_task))
                    except asyncio.CancelledError:
                        # A thread cannot be cancelled: keep the gate until the
                        # filesystem operation actually finishes.
                        try:
                            await write_task
                        except Exception:
                            pass
                        raise
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
            except (ValueError,httpx.HTTPError,CloudError,UncertainState) as exc:
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
