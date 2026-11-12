"""Task-scoped leaf-first empty-directory cleanup; never recursively delete."""
import asyncio
import json
import time
from pathlib import Path

from .cloud115 import CloudError, fatal
from .media import METADATA, SUBTITLES
from .organize_options import library_root
from .recycle import match_record


class EmptyDirectoryCleanup:
    def __init__(self, engine):
        self.engine = engine

    @property
    def cloud(self):
        return self.engine.cloud

    @staticmethod
    def needs_candidate_retry(state):
        if 'retry_candidates' in state:
            return bool(state['retry_candidates'])
        # 0.8.33 and earlier stored only the candidate warning, without a
        # retry marker. Preserve a route to retry those existing tasks too.
        warning = str(state.get('warning') or '')
        return '目录读取失败' in warning or '删除状态未能确认' in warning

    @staticmethod
    def retryable(job):
        config = json.loads(job['config'])
        state = config.get('empty_cleanup')
        return bool(job['state'] == 'partial' and config.get('job_kind', 'organize') == 'organize'
                    and not config.get('repair_only') and isinstance(state, dict) and state.get('warning')
                    and (EmptyDirectoryCleanup.needs_candidate_retry(state) or any(e.get('state') in ('intent', 'pending') for e in state.get('entries', []))))

    def save(self, job_id, state):
        config = json.loads(self.engine.job(job_id)['config'])
        config['empty_cleanup'] = state
        self.engine.update_job(job_id, config=json.dumps(config, ensure_ascii=False))
        account = self.engine.index.account(config)
        with self.engine.store.db:
            self.engine.store.db.executemany('DELETE FROM directory_cache WHERE account_id=? AND id=?',[(account,e['source']['id']) for e in state.get('entries',[]) if e.get('state')=='confirmed'])

    async def gone(self, cid):
        """目录是否已经从 115 上消失（用来区分「读不到」和「已经不存在」）。"""
        check = getattr(self.cloud, 'live_file_exists', None)
        if not callable(check):
            return False
        try:
            return not await check(cid)
        except CloudError:
            return False

    async def candidates(self, job_id, config, rows):
        """列出可回收的空目录候选；读不到的目录只跳过，不拖垮整轮清理。

        115 偶尔会在 `cid=X` 的响应里混进别的目录条目（分页/WAF 抖动），
        旧实现直接抛 DIRECTORY_CHANGED，整轮清理就没了——任务只会留一句
        「相关空目录清理未完成」。这里改成逐个容错：失败的目录记为跳过并带上警告，
        其余目录照常处理，宁可少清一个也不误删。
        """
        starts = set()
        for row in rows:
            if row['state'] not in ('done','recycled'):
                continue
            original, plan = json.loads(row['original']), json.loads(row['plan'])
            starts.add(original['parent_id'])
            for old in plan.get('wash', {}).get('old', []):
                starts.add(old['source']['parent_id'])
        # Explicitly received packages may contain empty siblings without any
        # video row; these are candidates only after a complete batch scan.
        if config.get('source_entries') is not None:
            starts.update(str(cid) for cid in config.get('scan_bundle_dirs', []) if str(cid).isdigit())
        found = {}
        skipped = 0
        immediate = {str(cid) for cid in config.get('immediate_cleanup_ids', [])}
        source_roots = config.get('cleanup_source_cids', config['source_cids'])
        roots = set(source_roots) | {config['archive_cid'], config['pending_cid'], '0'}
        for cid in sorted(starts):
            self.engine.boundary(job_id)
            if cid in roots:
                continue
            if str(cid) in immediate:
                # A successful recycle request is not proof of absence. Check
                # the live namespace first and never recycle this ID twice.
                try:
                    exists = await self.cloud.live_file_exists(cid)
                except CloudError as exc:
                    if fatal(exc):
                        raise
                    self.engine.store.event('organize', f'任务 {job_id}：已提交回收的目录 {cid} 状态无法核实（{exc.code}）。')
                    skipped += 1
                    continue
                if not exists:
                    continue
                self.engine.store.event('organize', f'任务 {job_id}：已提交回收的目录 {cid} 仍存在或已恢复，保留并停止重复删除。')
                skipped += 1
                continue
            try:
                trail = await self.cloud.directory(cid)
            except CloudError as exc:
                if exc.code not in ('DIRECTORY_CHANGED', 'INCOMPLETE_LIST', 'DIRECTORY_UNAVAILABLE'):
                    raise
                if exc.code == 'DIRECTORY_UNAVAILABLE' and await self.gone(cid):
                    # 目录已经不存在了：上一轮清掉、或别人删了，目标都已达成。
                    # 记成「读取失败」会让任务一直挂在「部分未完成」，而实际没有东西要清。
                    self.engine.store.event('organize', f'任务 {job_id}：清理目录 {cid} 已不存在，跳过。')
                    continue
                self.engine.store.event('organize', f'任务 {job_id}：清理目录 {cid} 读取失败（{exc.code}）。')
                skipped += 1
                continue
            source_root = next((r for r in source_roots if r in trail), None)
            archive_root = config['archive_cid'] if config['archive_cid'] in trail else None
            root = source_root or archive_root
            if root is None:
                continue
            # Archive category and region directories are permanent library roots.
            first = trail.index(root) + (1 if source_root else 3)
            for index in range(len(trail)-1, first-1, -1):
                candidate, parent = trail[index], trail[index-1]
                if candidate in roots or candidate in found:
                    continue
                try:
                    entries = [e for e in await self.cloud.children(parent) if e['id'] == candidate]
                except CloudError as exc:
                    if exc.code not in ('DIRECTORY_CHANGED', 'INCOMPLETE_LIST', 'DIRECTORY_UNAVAILABLE'):
                        raise
                    if exc.code == 'DIRECTORY_UNAVAILABLE' and await self.gone(parent):
                        self.engine.store.event('organize', f'任务 {job_id}：清理目录 {candidate} 的父目录已不存在，跳过。')
                        continue
                    self.engine.store.event('organize', f'任务 {job_id}：清理目录 {candidate} 的父目录读取失败（{exc.code}）。')
                    skipped += 1
                    continue
                if len(entries) != 1 or not entries[0]['is_dir']:
                    raise CloudError('相关目录身份无法核实，保留空目录。', 'DIRECTORY_CHANGED', 409)
                found[candidate] = {'source': {'id':candidate, 'parent_id':parent,
                    'name':entries[0]['name'], 'size':0, 'is_dir':True},
                    'trail':trail[:index+1], 'root':root, 'state':'pending'}
        warning = ('%d 个目录读取失败或删除状态未能确认，请检查任务日志及目录。' % skipped) if skipped else ''
        return sorted(found.values(), key=lambda c: len(c['trail']), reverse=True), warning

    async def confirm(self, entry):
        source = entry['source']
        # Visibility can lag the successful mutation. Retry reads only; the
        # persisted intent never permits another recycle request for this ID.
        for attempt in range(3):
            record = match_record(source, entry, await self.cloud.recycle_files())
            if record is not None:
                break
            if attempt == 2:
                raise CloudError('空目录回收记录尚未唯一关联；保留意图，不重复请求。', 'RECYCLE_UNVERIFIED', 409)
            await asyncio.sleep(.35 * (attempt + 1))
        # A uniquely bound recycle record plus absence from the live namespace
        # confirms this exact ID. Listing its entire parent adds no identity
        # evidence and can fail for unrelated pagination after the mutation.
        if await self.cloud.live_file_exists(source['id']):
            raise CloudError('空目录已被恢复或移动，停止清理。', 'DIRECTORY_CHANGED', 409)
        entry.update(state='confirmed', rid=record['rid'], observed=record)


    async def confirm_subtitle(self, action):
        source = action['source']
        for attempt in range(3):
            record = match_record(source, action, await self.cloud.recycle_files())
            if record is not None:
                break
            if attempt == 2:
                raise CloudError('字幕回收结果未能唯一确认，保留记录并停止重复提交。', 'RECYCLE_UNVERIFIED', 409)
            await asyncio.sleep(.35 * (attempt + 1))
        if await self.cloud.live_file_exists(source['id']):
            raise CloudError('字幕仍存在或已恢复，停止清理。', 'RECYCLE_UNVERIFIED', 409)
        action.update(state='confirmed', rid=record['rid'])

    async def recycle_orphan_subtitle(self, job_id, entry, state, source, snapshot, metadata_snapshot):
        actions = entry.setdefault('subtitle_actions', [])
        action = next((a for a in actions if a['source']['id'] == source['id']), None)
        if action:
            if action['state'] == 'intent':
                await self.confirm_subtitle(action)
                self.save(job_id, state)
            return
        actual = await self.cloud.file(source['id'])
        for key in ('id','name','parent_id','size'):
            if actual.get(key) != source.get(key):
                raise CloudError('字幕身份或位置发生变化，已保留。', 'FILE_CHANGED', 409)
        if source.get('sha1') and (actual.get('sha1') or '').upper() != source['sha1'].upper():
            raise CloudError('字幕内容身份发生变化，已保留。', 'FILE_CHANGED', 409)
        before = await self.cloud.recycle_files()
        fresh_children = getattr(self.cloud, 'fresh_children', self.cloud.children)
        fresh = await fresh_children(source['parent_id'])
        if not any(str(item.get('id')) == str(source['id']) for item in fresh):
            raise CloudError('字幕已不在原目录，保留记录并停止清理。', 'FILE_CHANGED', 409)
        for item in fresh:
            suffix = Path(item['name']).suffix.lower()
            if item['is_dir'] or suffix not in (METADATA | SUBTITLES):
                raise CloudError('目录新增其他内容，保留字幕。', 'DIRECTORY_CHANGED', 409)
            if suffix in SUBTITLES:
                saved = snapshot.get(str(item['id']))
                if not saved or any(item.get(k) != saved.get(k) for k in ('id','name','parent_id','size')):
                    raise CloudError('目录字幕发生变化，已保留。', 'DIRECTORY_CHANGED', 409)
            if suffix in METADATA:
                saved = metadata_snapshot.get(str(item['id']))
                if not self.metadata_matches(item, saved):
                    raise CloudError('目录元数据发生变化，已保留。', 'DIRECTORY_CHANGED', 409)
        action = {'source': {**source, 'is_dir': False}, 'state':'intent', 'at':time.time(),
                  'before_rids':[item['rid'] for item in before]}
        actions.append(action)
        self.save(job_id, state)
        self.engine.audit(job_id, 0, '无法跟随的字幕回收意图：' + source['name'])
        self.engine.boundary(job_id)
        try:
            await self.cloud.recycle(source['id'], source['parent_id'])
        except CloudError as exc:
            if fatal(exc): raise
        await self.confirm_subtitle(action)
        self.save(job_id, state)
        self.engine.audit(job_id, 0, '无法跟随的字幕已回收：' + source['name'])


    @staticmethod
    def metadata_matches(actual, expected):
        if not expected or not expected.get('sha1'):
            return False
        return (all(actual.get(k) == expected.get(k) for k in ('id', 'name', 'parent_id', 'size'))
                and str(actual.get('sha1') or '').upper() == str(expected['sha1']).upper())

    async def recycle_metadata(self, job_id, entry, state, source, metadata_snapshot, subtitle_snapshot):
        actions = entry.setdefault('metadata_actions', [])
        action = next((a for a in actions if str(a['source']['id']) == str(source['id'])), None)
        if action:
            if action['state'] == 'intent':
                await self.confirm_subtitle(action)
                self.save(job_id, state)
            return
        actual = await self.cloud.file(source['id'])
        if not self.metadata_matches(actual, source):
            raise CloudError('元数据身份或内容发生变化，已保留。', 'FILE_CHANGED', 409)
        before = await self.cloud.recycle_files()
        fresh_children = getattr(self.cloud, 'fresh_children', self.cloud.children)
        fresh = await fresh_children(source['parent_id'])
        if not any(str(item['id']) == str(source['id']) and self.metadata_matches(item, source) for item in fresh):
            raise CloudError('元数据不在原目录或身份变化，已保留。', 'FILE_CHANGED', 409)
        for item in fresh:
            suffix = Path(item['name']).suffix.lower()
            if item['is_dir'] or suffix not in (METADATA | SUBTITLES):
                raise CloudError('目录新增其他内容，已保留。', 'DIRECTORY_CHANGED', 409)
            if suffix in METADATA and not self.metadata_matches(item, metadata_snapshot.get(str(item['id']))):
                raise CloudError('目录元数据发生变化，已保留。', 'DIRECTORY_CHANGED', 409)
            if suffix in SUBTITLES:
                saved = subtitle_snapshot.get(str(item['id']))
                if not saved or any(item.get(k) != saved.get(k) for k in ('id', 'name', 'parent_id', 'size')):
                    raise CloudError('目录字幕发生变化，已保留。', 'DIRECTORY_CHANGED', 409)
        action = {'source': {**source, 'is_dir': False}, 'state': 'intent', 'at': time.time(),
                  'before_rids': [item['rid'] for item in before]}
        actions.append(action)
        self.save(job_id, state)
        self.engine.audit(job_id, 0, '回收扫描时元数据意图：' + source['name'])
        self.engine.boundary(job_id)
        try:
            await self.cloud.recycle(source['id'], source['parent_id'])
        except CloudError as exc:
            if fatal(exc): raise
        await self.confirm_subtitle(action)
        self.save(job_id, state)
        self.engine.audit(job_id, 0, '扫描时元数据已回收：' + source['name'])


    def local(self, rows):
        root = self.engine.root
        # 0.8.30：库根不再写死「影视媒体库」，跟着当前整理配置解析出来的库根走
        # （115 目录叫「媒体库」时，白名单会让洗版留下的空目录永远清不掉）。
        library = library_root(self.engine.store.get('organize_config') or {})
        paths = set()
        for row in rows:
            if row['state'] != 'done':
                continue
            for old in json.loads(row['plan']).get('wash', {}).get('old', []):
                for relative in old.get('local_files', {}):
                    path = root / relative
                    if not path.resolve().is_relative_to(root):
                        continue
                    for parent in path.parents:
                        if parent == root or not parent.is_relative_to(root):
                            break
                        parts = parent.relative_to(root).parts
                        if len(parts) <= 3 or parts[0] != library:
                            break
                        paths.add(parent)
        for path in sorted(paths, key=lambda p:len(p.parts), reverse=True):
            # rmdir is atomic and fails if any subtitle, NFO, image or other item
            # remains. Never follow directory symlinks or call rmtree.
            if path.is_symlink():
                continue
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                continue

    async def run(self, job_id, config):
        if config.get('repair_only'):
            return {'removed':0,'warning':''}
        rows = self.engine.store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (job_id,)).fetchall()
        current_config = json.loads(self.engine.job(job_id)['config'])
        fresh_children = getattr(self.cloud, 'fresh_children', self.cloud.children)
        state = current_config.get('empty_cleanup')
        try:
            if state is None:
                entries, warning = await self.candidates(job_id, current_config, rows)
                state = {'entries': entries, 'warning': warning, 'retry_candidates': bool(warning)}
                self.save(job_id, state)
            elif self.needs_candidate_retry(state):
                # Re-enumeration may discover new candidates but must never
                # reset a persisted delete intent or confirmed recycle.
                entries, warning = await self.candidates(job_id, current_config, rows)
                existing = {str(e['source']['id']) for e in state.get('entries', [])}
                state.setdefault('entries', []).extend(e for e in entries if str(e['source']['id']) not in existing)
                state['entries'].sort(key=lambda e: len(e['trail']), reverse=True)
                state['warning'], state['retry_candidates'] = warning, bool(warning)
                self.save(job_id, state)
            else:
                # Re-evaluate old transient failures during an explicit retry.
                state['warning'] = ''
            for entry in state['entries']:
                self.engine.boundary(job_id)
                if entry['state'] in ('confirmed','retained','missing'):
                    continue
                source = entry['source']
                if entry['state'] == 'intent':
                    await self.confirm(entry)
                    self.save(job_id, state)
                    continue
                listing = await fresh_children(source['parent_id'])
                matches = [e for e in listing if e['id'] == source['id']]
                if not matches:
                    entry['state'] = 'missing'
                    self.save(job_id, state)
                    continue
                if (len(matches) != 1 or not matches[0]['is_dir'] or matches[0]['name'] != source['name']):
                    raise CloudError('相关空目录名称或位置发生变化，停止清理。', 'DIRECTORY_CHANGED', 409)
                # Complete uncertain prior attempts without issuing a second delete.
                for action in entry.get('subtitle_actions', []) + entry.get('metadata_actions', []):
                    if action.get('state') == 'intent':
                        await self.confirm_subtitle(action)
                        self.save(job_id, state)
                if entry.get('metadata_actions'):
                    entry['removed_files'] = sum(a.get('state') == 'confirmed' for a in entry['metadata_actions'])
                    self.save(job_id, state)
                leftovers = await fresh_children(source['id'])
                if leftovers:
                    attachments = [e for e in leftovers if not e['is_dir'] and Path(e['name']).suffix.lower() in METADATA]
                    subtitles = [e for e in leftovers if not e['is_dir'] and Path(e['name']).suffix.lower() in SUBTITLES]
                    unknown = [e for e in leftovers if e['is_dir'] or Path(e['name']).suffix.lower() not in (METADATA | SUBTITLES)]
                    snapshot = {str(e.get('id')):e for e in current_config.get('orphan_subtitle_snapshot', [])
                                if str(e.get('parent_id')) == str(source['id'])}
                    metadata_snapshot = {str(e.get('id')): e for e in current_config.get('cleanup_metadata_snapshot', [])
                                         if str(e.get('parent_id')) == str(source['id'])}
                    unresolved = any(r['state'] not in ('done','recycled') and
                                     str(json.loads(r['original']).get('parent_id')) == str(source['id']) for r in rows)
                    # A skipped ISO, unknown file/subdirectory, later arrival or
                    # unfinished video makes subtitle ownership ambiguous.
                    eligible = bool(subtitles) and not unresolved and all(
                        str(e['id']) in snapshot and all(e.get(k) == snapshot[str(e['id'])].get(k)
                        for k in ('id','name','parent_id','size')) for e in subtitles)
                    if (unknown or (subtitles and not eligible) or (attachments and unresolved)
                            or any(not self.metadata_matches(e, metadata_snapshot.get(str(e['id']))) for e in attachments)):
                        entry['state'] = 'retained'
                        self.save(job_id, state)
                        continue
                    for item in subtitles:
                        self.engine.boundary(job_id)
                        await self.recycle_orphan_subtitle(job_id, entry, state, snapshot[str(item['id'])], snapshot, metadata_snapshot)
                    for item in attachments:
                        self.engine.boundary(job_id)
                        await self.recycle_metadata(job_id, entry, state, metadata_snapshot[str(item['id'])], metadata_snapshot, snapshot)
                    entry['removed_files'] = sum(a.get('state') == 'confirmed' for a in entry.get('metadata_actions', []))
                    self.save(job_id, state)
                    if await fresh_children(source['id']):
                        entry['state'] = 'retained'
                        self.save(job_id, state)
                        continue
                cached = getattr(self.cloud, 'recycle_snapshot', None)
                before = (cached() if callable(cached) else None) or await self.cloud.recycle_files()
                entry.update(state='intent', at=time.time(), before_rids=[e['rid'] for e in before])
                self.save(job_id, state)
                self.engine.audit(job_id, 0, '单个空目录回收意图：' + source['id'] + ' / ' + source['name'])
                self.engine.boundary(job_id)
                try:
                    await self.cloud.recycle_empty_directory(source['id'], source['parent_id'])
                except CloudError as exc:
                    if fatal(exc):
                        raise
                # Finish readback even when pause/cancel arrived in flight.
                await self.confirm(entry)
                self.save(job_id, state)
                self.engine.audit(job_id, 0, '空目录回收已核实：' + source['id'])
            # 保留候选阶段记下的「N 个目录读取失败」警告：这正是「重试空目录清理」的入口，
            # 清空它会让跳过的目录失去重试机会。
            state.setdefault('warning', '')
            self.save(job_id, state)
        except CloudError as exc:
            if exc.code in ('LOGIN_REQUIRED','LOGIN_INVALID'):
                raise
            if state is None:
                state = {'entries':[], 'warning':'', 'retry_candidates':True}
            state['warning'] = '视频处理结果已保留；相关空目录清理未完成：' + str(exc)
            self.save(job_id, state)
        self.local(rows)
        return {'removed':sum(e['state']=='confirmed' for e in state['entries']),
                'attachments':sum(e.get('removed_files',0) for e in state['entries']),
                'subtitles':sum(a.get('state')=='confirmed' for e in state['entries'] for a in e.get('subtitle_actions', [])),
                'warning':state['warning']}
