"""Durable per-file replacement: publish new, recycle exact old IDs, finalize names."""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from .cloud115 import CloudError, fatal
from .media import NeedsReview, SUBTITLES, VIDEOS, media_from_filename, subtitle_suffix
from .playaddr import candidates
from .quality import UpgradePolicy, compare, facts, group_key, version_layout
from .recycle import match_record


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def short_value(value):
    """把比较证据里的原始值压成能读的一小段（ 文案里「胜者>败者」那种）。"""
    if isinstance(value, dict):
        text = value.get('codec') or value.get('type') or value.get('platform') or ''
        channels = value.get('channels')
        text = str(text or '')
        if channels:
            text = (text + ' ' + str(channels)).strip()
        return text or '未识别'
    if value is None or value == '':
        return '未识别'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return '是' if value else '否'
    return str(value)


_RES_LABEL = {'4320': '4320p / 8K', '2160': '2160p / 4K', '1440': '1440p / 2K', '1080': '1080p'}


def resolution_text(value):
    text = short_value(value)
    if text == '未识别':
        return text
    return _RES_LABEL.get(text, text + 'p')


def wash_reason(wash):
    """洗版胜负的一句话证据，对齐 ：洗版覆盖：<规则>优先级(<胜者><败者>)。"""
    for comparison in wash.get('comparisons') or []:
        for rule in comparison.get('rules') or []:
            if rule.get('result') == 1:
                new, old = short_value(rule.get('new')), short_value(rule.get('old'))
                if rule.get('rule') == 'resolution':
                    new, old = resolution_text(rule.get('new')), resolution_text(rule.get('old'))
                return '洗版覆盖：%s优先级(%s>%s)' % (
                    rule.get('label') or rule.get('rule'), new, old)
    return ''


def uncertain(message):
    return CloudError(message, 'UPGRADE_UNCERTAIN', 409)


class Upgrader:
    def __init__(self, engine):
        self.engine, self.store, self.cloud = engine, engine.store, engine.cloud

    def save(self, item_id, plan, step):
        self.engine.update_item(item_id, plan=plan, step=step)

    def local_path(self, relative):
        path = self.engine.root / relative
        if not path.resolve().is_relative_to(self.engine.root):
            raise uncertain('洗版本地路径越界，停止清理。')
        return path

    def owned_files(self, row):
        plan = json.loads(row['plan'])
        owned = dict(plan.get('local_files', {}))
        # Old releases recorded the exact STRM URL intent but not subtitle hashes.
        # Retain legacy subtitles; never infer ownership from a filename alone.
        if plan.get('relative_path') and plan.get('token_cipher'):
            token = self.store.crypto.decrypt(plan['token_cipher'].encode()).decode()
            base = plan.get('playback_base') or self.store.get('playback_base') or ''
            # 地址格式（是否有可读段）由 playaddr 一处给出；旧格式仍在候选里，
            # 因此升级前后写下的同一播放身份都能被认领。
            forms = candidates(base, plan['playback_id'], Path(plan['relative_path']).stem, token)
            expected = hashlib.sha256(forms[0]).hexdigest()
            local = self.engine.root / plan['relative_path']
            try:
                # 本地文件若恰好是其中一种合法形态，就以磁盘实际字节登记，
                # 避免「登记的是旧格式、磁盘已是新格式」这类历史偏差。
                if local.is_file() and local.stat().st_size <= 20 * 1024 * 1024:
                    actual = hashlib.sha256(local.read_bytes()).hexdigest()
                    if any(hashlib.sha256(form).hexdigest() == actual for form in forms):
                        expected = actual
            except OSError:
                pass
            owned.setdefault(plan['relative_path'], expected)
        return owned

    def allow_local_write(self, path, plan):
        if not path.resolve().is_relative_to(self.engine.root):
            raise uncertain('新输出目标路径越界，停止覆盖。')
        if not plan.get('wash') or not path.is_file():
            return
        relative = str(path.relative_to(self.engine.root))
        owners = dict(plan.get('local_files', {}))
        for old in plan['wash'].get('old', []):
            owners.update({k: v for k, v in old['local_files'].items() if k not in owners})
        expected = owners.get(relative)
        if not expected or path.stat().st_size > 20 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise uncertain('新输出目标已有未确认归属或已修改的本地文件，保留文件并停止覆盖。')

    async def prepare(self, job_id, original, plan, config):
        policy = UpgradePolicy.model_validate(config.get('upgrade_policy', {})).model_dump()
        # Existing snapshots predate this behavior. Only explicitly snapshotted
        # support may enable content deduplication when a task resumes.
        policy['deduplicate_identical'] = config.get('upgrade_policy', {}).get('deduplicate_identical') is True
        candidates = await self.engine.index.candidates(job_id, original, plan)
        mode = policy['mode']
        if candidates and mode == 'pending':
            raise NeedsReview('当前归档目录已有同片或重叠集数；按配置保留旧版，新文件转待处理。')
        if candidates and mode == 'skip':
            plan['wash'] = {'decision': 'skip', 'reason': '已有冲突，按配置原位置跳过。', 'actions': {}}
            return
        if mode != 'upgrade':
            return
        if policy['deduplicate_identical'] and await self.prepare_duplicate(job_id, original, plan, config, candidates, policy):
            return
        new_facts = facts(original['name'], plan['media'], original['size'])
        unknown_new_dv = policy['dynamic_groups'] == 'dovi' and new_facts['dynamic_range'] is None
        base_policy = {**policy, 'dynamic_groups': 'off'}
        base_group = group_key(new_facts, base_policy)
        group = '.'.join(filter(None, (base_group, 'UnknownDoVi'))) if unknown_new_dv else group_key(new_facts, policy)
        version_layout(plan, original['name'], group, policy)
        wash = {'decision': 'archive', 'group': group, 'policy': policy,
                'rule_version': hashlib.sha256(encoded(policy).encode()).hexdigest()[:16],
                'new_facts': new_facts, 'comparisons': [], 'old': [], 'actions': {}}
        winner = None
        for entry, current in candidates:
            hint = plan['hint']
            if hint['media_type'] == 'tv' and (entry['episode'], entry['end_episode']) != (hint['episode'], hint['end_episode']):
                raise NeedsReview('合并集与单集范围仅有重叠，禁止互相淘汰，转待处理。')
            self.engine.boundary(job_id)
            managed = self.store.db.execute('SELECT i.plan,i.original FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=?', (current['id'],)).fetchone()
            raw_name = current['name']
            if managed:
                old_plan, old_original = json.loads(managed['plan']), json.loads(managed['original'])
                if old_plan.get('dest') != current['parent_id'] or old_plan.get('target_name') != current['name'] or old_original.get('size') != current['size']:
                    managed = None  # An externally moved/renamed file is a cache miss.
                else:
                    raw_name = old_original['name']
            media, raw_name, _ = self.engine.measurements.comparison_facts(job_id, current, raw_name)
            old_facts = facts(raw_name, media, current['size'])
            old_base = group_key(old_facts, base_policy)
            unknown_old_dv = policy['dynamic_groups'] == 'dovi' and old_facts['dynamic_range'] is None
            old_group = '.'.join(filter(None, (old_base, 'UnknownDoVi'))) if unknown_old_dv else group_key(old_facts, policy)
            if old_base != base_group or (not unknown_new_dv and not unknown_old_dv and old_group != group):
                wash['comparisons'].append({'file_id': current['id'], 'result': 'coexist', 'old_group': old_group, 'rules': []})
                continue
            try:
                if unknown_new_dv or unknown_old_dv:
                    result = 0 if unknown_new_dv else 1
                    evidence = [{'rule': 'dynamic_group', 'result': 'unknown', 'reason': '杜比分组未知，按策略优先保留可确认分组；同为未知保留旧版。'}]
                else:
                    result, evidence = compare(new_facts, old_facts, policy, skip_unknown=True)
            except NeedsReview as exc:
                # 把比对的另一方写进消息：只报「动态范围缺失」无法判断是哪一边。
                raise NeedsReview(f'{exc}（比较对象：{raw_name}）') from None
            wash['comparisons'].append({'file_id': current['id'], 'result': result, 'old_group': old_group, 'rules': evidence})
            saved = {k: current[k] for k in ('id', 'name', 'parent_id', 'size', 'sha1') if k in current}
            old = {'source': saved, 'facts': old_facts, 'subtitles': [], 'local_files': {}, 'playback_id': ''}
            account = self.engine.index.account(config)
            output = self.engine.outputs.get(account, current['id'])
            if output:
                saved_output = json.loads(output['source'])
                if (saved_output.get('size') == current['size'] and
                        saved_output.get('sha1', '').upper() == current.get('sha1', '').upper()):
                    old['local_files'] = json.loads(output['files'])
                    old['playback_id'] = output['playback_id']
            if managed and result > 0:
                old['local_files'] = self.owned_files(managed)
                old['playback_id'] = old_plan.get('playback_id', '')
                for sub in old_plan.get('subtitles', []):
                    # Only subtitles explicitly bound to this video are candidates.
                    if sub['source']['id'] == current['id']:
                        raise uncertain('旧字幕记录与视频 ID 冲突。')
                    shared = False
                    for other in self.store.db.execute('SELECT i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id<>?', (current['id'],)):
                        if any(s['source']['id'] == sub['source']['id'] for s in json.loads(other['plan']).get('subtitles', [])):
                            shared = True
                            break
                    if not shared:
                        old['subtitles'].append({**sub['source'], 'name': sub['target_name'], 'parent_id': old_plan['dest']})
            if result <= 0:
                winner = old
            else:
                wash['old'].append(old)
        if winner:
            wash.update(decision='discard', winner=winner)
            winning = next((c for c in reversed(wash['comparisons']) if c['file_id'] == winner['source']['id']), {})
            if winning.get('result') == 0:
                wash['reason'] = 'unknown_fallback' if any(r.get('result') == 'unknown' for r in winning.get('rules', [])) else 'equal_rules'
            wash['old'] = []
        elif wash['old']:
            wash['decision'] = 'replace'
            suffix = Path(plan['target_name']).suffix
            wash['stage_name'] = Path(plan['target_name']).stem + '.sknew-' + original['id'] + suffix
            if len(wash['stage_name'].encode()) > 250:
                raise NeedsReview('洗版临时文件名超长，保留新旧资源待处理。')
        plan['wash'] = wash

    @staticmethod
    def content_key(source):
        digest, size = source.get('sha1'), source.get('size')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{40}', digest):
            return None
        if type(size) is not int or size <= 0:
            return None
        return digest.casefold(), size

    async def prepare_duplicate(self, job_id, original, plan, config, candidates, policy):
        if not candidates:
            return False
        hint = plan['hint']
        if hint['media_type'] == 'tv' and any(
                (entry['episode'], entry['end_episode']) != (hint['episode'], hint['end_episode'])
                for entry, _ in candidates):
            raise NeedsReview('合并集与单集范围仅有重叠，禁止按重复副本自动回收。')
        current_new = await self.verify(job_id, original, config['source_cids'])
        key = self.content_key(current_new)
        if key is None:
            return False
        for _, candidate in candidates:
            # Both fingerprints come from fresh, scope-checked file details,
            # not cached archive listings or quality-comparison scores.
            current_old = await self.verify(job_id, candidate, [config['archive_cid']])
            if current_old['id'] == original['id'] or self.content_key(current_old) != key:
                continue
            saved = {k: current_old[k] for k in ('id', 'name', 'parent_id', 'size', 'sha1')}
            original['sha1'] = current_new['sha1']
            plan['wash'] = {'decision': 'discard', 'reason': 'identical_copy', 'group': 'identical',
                'policy': policy, 'rule_version': hashlib.sha256(encoded(policy).encode()).hexdigest()[:16],
                'winner': {'source': saved}, 'duplicate': {'sha1': key[0], 'size': key[1], 'subtitles': [], 'discard_bound_subtitles': True,
                                                          'subtitle_ids': [s['id'] for s in original.get('subtitles', [])]},
                'old': [], 'actions': {}, 'comparisons': [{'file_id': current_old['id'],
                    'result': 'identical', 'rules': [{'rule': 'content', 'label': '内容 SHA1 与精确字节数',
                        'new': {'sha1': key[0], 'bytes': key[1]}, 'old': {'sha1': key[0], 'bytes': key[1]}, 'result': 0}]}]}
            return True
        return False

    async def plan_duplicate_subtitles(self, job_id, original, winner, config):
        subs = original.get('subtitles', [])
        if not subs:
            return {'subtitles': []}
        row = self.store.db.execute('SELECT o.item_id,o.relative_path,i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=?', (winner['id'],)).fetchone()
        if not row:
            raise uncertain('原版尚无受管理的 STRM 位置，先补齐原版输出，再合并新字幕。')
        old_plan = json.loads(row['plan'])
        if (old_plan.get('dest') != winner['parent_id'] or old_plan.get('target_name') != winner['name']
                or old_plan.get('relative_path') != row['relative_path']):
            raise uncertain('原版的字幕/STRM 位置与归档记录不一致，停止回收副本。')
        relative = Path(row['relative_path'])
        self.local_path(str(relative))
        listing = await self.cloud.children(winner['parent_id'])
        stem = Path(winner['name']).stem
        if any(e['id'] != winner['id'] and not e['is_dir'] and Path(e['name']).suffix.lower() in VIDEOS
               and Path(e['name']).stem == stem for e in listing):
            raise uncertain('原版旁有同主体多视频，无法明确归属字幕，停止回收副本。')
        planned, names = [], set()
        for sub in subs:
            current = await self.verify(job_id, sub, config['source_cids'])
            key = self.content_key(current)
            if key is None or current['size'] > 20 * 1024 * 1024:
                raise uncertain('新字幕缺少可靠内容指纹或超出同步限制，视频与字幕均保留待核查。')
            suffix = subtitle_suffix(original['name'], current['name'])
            target = stem + suffix
            if (not suffix.startswith('.')
                    or Path(target).suffix.lower() not in SUBTITLES or len(target.encode()) > 250 or target in names):
                raise uncertain('新字幕的名称/关联不唯一，停止回收副本。')
            names.add(target)
            matches = [e for e in listing if e['name'] == target]
            if len(matches) > 1 or any(e['is_dir'] for e in matches):
                raise uncertain('原版目标字幕重名或变成目录，停止回收副本。')
            keeper = {**current, 'name': target, 'parent_id': winner['parent_id']}
            action = 'move'
            if matches:
                keeper = await self.verify(job_id, matches[0], [config['archive_cid']])
                if self.content_key(keeper) != key:
                    raise uncertain('原版已有同名但内容不同的字幕，保留两端视频与字幕，等待核查。')
                action = 'recycle'
            local = str(relative.parent / target)
            self.local_path(local)
            planned.append({'source': current, 'keeper': keeper, 'action': action,
                            'relative_path': local, 'sha1': key[0], 'size': key[1]})
        return {'subtitles': planned, 'managed_item_id': row['item_id'], 'managed_relative_path': str(relative)}

    async def sync_duplicate_subtitles(self, job_id, item_id, plan, config):
        proof, winner = plan['wash']['duplicate'], plan['wash']['winner']['source']
        async def retained_guard():
            actual = await self.verify(job_id, winner, [config['archive_cid']])
            self.verify_duplicate_source(plan, actual)
        for sub in proof.get('subtitles', []):
            await retained_guard()
            source, keeper = sub['source'], sub['keeper']
            if sub['action'] == 'move':
                # change_file accepts exactly the recorded source/target states,
                # so a restart after rename or move does not duplicate mutations.
                current = await self.cloud.file(source['id'])
                if (current['name'] not in (source['name'], keeper['name']) or
                        current['parent_id'] not in (source['parent_id'], keeper['parent_id'])):
                    raise uncertain('新字幕名称或位置发生外部变化，停止回收视频。')
                current = await self.verify(job_id, {**source, 'name': current['name'], 'parent_id': current['parent_id']},
                    [config['archive_cid']] if current['parent_id'] == keeper['parent_id'] else config['source_cids'])
                if self.content_key(current) != (sub['sha1'], sub['size']):
                    raise uncertain('新字幕指纹变化，停止回收视频。')
            else:
                current = await self.verify(job_id, keeper, [config['archive_cid']])
            if self.content_key(current) != (sub['sha1'], sub['size']):
                raise uncertain('保留字幕的内容指纹已变化，停止回收视频。')
            ua = 'StrimKeep/0.4 Duplicate Subtitle'
            url, headers = await self.cloud.url(current['pickcode'], ua)
            try:
                content = await self.engine.download(url, {**headers, 'User-Agent': ua}, 20 * 1024 * 1024)
            except (httpx.HTTPError, ValueError, CloudError) as exc:
                # Do not turn a subtitle transport failure into an orphaned video.
                if isinstance(exc, CloudError) and exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    raise
                raise uncertain('重复副本的字幕同步失败，视频尚未回收；恢复连接后核查继续。') from None
            if len(content) != sub['size'] or hashlib.sha1(content).hexdigest() != sub['sha1']:
                raise uncertain('字幕下载内容与网盘指纹/大小不一致，停止回收视频。')
            self.engine.boundary(job_id)
            local = self.local_path(sub['relative_path'])
            checksum = hashlib.sha256(content).hexdigest()
            if local.exists() and (not local.is_file() or local.stat().st_size > 20 * 1024 * 1024
                    or hashlib.sha256(local.read_bytes()).hexdigest() != checksum):
                raise uncertain('本地目标字幕已有不同内容，保留文件并停止回收视频。')
            sub['local_sha256'] = checksum
            self.save(item_id, plan, '正在同步字幕')
            if not local.is_file():
                # Lazy import avoids the Organizer/Upgrader module cycle.
                from .core import atomic_write
                atomic_write(local, content, self.engine.root)
            await retained_guard()
            if sub['action'] == 'move':
                await self.engine.change_file(job_id, item_id, source, keeper['name'], keeper['parent_id'], config['retries'])
            await self.verify_duplicate_subtitle(job_id, sub, config)
            self.bind_duplicate_subtitle(plan, sub)
            sub['ready'] = True
            self.save(item_id, plan, '字幕已归并并同步至原版')
            if sub['action'] == 'recycle':
                async def subtitle_guard():
                    await retained_guard()
                    await self.verify_duplicate_subtitle(job_id, sub, config)
                await self.recycle_one(job_id, item_id, plan, source, config['source_cids'], subtitle_guard)

    async def verify_duplicate_subtitle(self, job_id, sub, config):
        actual = await self.verify(job_id, sub['keeper'], [config['archive_cid']])
        if self.content_key(actual) != (sub['sha1'], sub['size']):
            raise uncertain('归并字幕的指纹不一致，停止回收视频。')
        local = self.local_path(sub['relative_path'])
        if (not local.is_file() or local.stat().st_size > 20 * 1024 * 1024
                or hashlib.sha256(local.read_bytes()).hexdigest() != sub.get('local_sha256')):
            raise uncertain('归并字幕的本地输出缺失或变化，停止回收视频。')

    def bind_duplicate_subtitle(self, plan, sub):
        proof, winner = plan['wash']['duplicate'], plan['wash']['winner']['source']
        with self.store.db:
            row = self.store.db.execute('SELECT i.plan FROM organized_files o JOIN organize_items i ON i.id=o.item_id WHERE o.file_id=? AND o.item_id=?', (winner['id'], proof['managed_item_id'])).fetchone()
            if not row:
                raise uncertain('原版管理记录已变化，停止回收视频。')
            old = json.loads(row['plan'])
            if (old.get('dest') != winner['parent_id'] or old.get('target_name') != winner['name']
                    or old.get('relative_path') != proof['managed_relative_path']):
                raise uncertain('原版管理路径已变化，停止回收视频。')
            existing = next((s for s in old['subtitles'] if s['source']['id'] == sub['keeper']['id']), None)
            if existing and existing['target_name'] != sub['keeper']['name']:
                raise uncertain('原版字幕绑定名称冲突，停止回收视频。')
            if not existing:
                old['subtitles'].append({'source': sub['keeper'], 'target_name': sub['keeper']['name']})
            old.setdefault('local_files', {})[sub['relative_path']] = sub['local_sha256']
            self.store.jobs.set_item_plan(proof['managed_item_id'], encoded(old))

    async def verify(self, job_id, source, scope):
        self.engine.boundary(job_id)
        current = await self.cloud.file(source['id'])
        if current.get('is_dir'):
            raise uncertain('洗版候选变为目录，禁止回收。')
        self.engine.check_source(source, current, source['name'], source['parent_id'])
        trail = await self.cloud.directory(source['parent_id'])
        if not set(scope) & set(trail):
            raise uncertain('洗版候选已离开已保存目录范围，停止修改。')
        return current

    async def recycle_snapshot(self):
        """回收前状态：优先复用上一份刚核对过的清单（30 秒内），否则全量读一次。

        删完一个文件后的那份清单就是下一个文件的回收前状态。云客户端不提供快照
        （例如测试里的替身）时，退回原来的全量读法。
        """
        cached = getattr(self.cloud, 'recycle_snapshot', None)
        rows = cached() if callable(cached) else None
        return rows or await self.cloud.recycle_files()

    async def recycle_recent(self, pages=3):
        """回收站最近若干页 + 是否读全；云客户端不支持时退回全量读。"""
        recent = getattr(self.cloud, 'recycle_recent', None)
        if callable(recent):
            return await recent(pages)
        return await self.cloud.recycle_files(), True

    async def recycled(self, source, action):
        # Read-only retries tolerate delayed visibility; never resubmit deletion.
        # 先只读最近几页：刚删的记录就在最前面；读不全再退回全量核对。
        for attempt in range(3):
            entries, complete = await self.recycle_recent(3)
            record = match_record(source, action, entries)
            if record is None and not complete:
                entries = await self.cloud.recycle_files()
                record = match_record(source, action, entries)
            if record:
                break
            if attempt == 2:
                raise CloudError('尚未唯一关联回收记录；保留回收站条目后核查继续，不会重复回收。', 'RECYCLE_REVIEW_REQUIRED', 409)
            await asyncio.sleep(.35 * (attempt + 1))
        await self.verify_retired(source)
        action['observed'] = record
        return record['rid']

    @staticmethod
    def source_digest(source):
        fields = {k: source.get(k) for k in ('id','name','size','parent_id','sha1')}
        return hashlib.sha256(encoded(fields).encode()).hexdigest()

    def confirm_action(self, job_id, plan, source, rid, journal='wash'):
        action = plan[journal]['actions'][source['id']]
        action.update(state='confirmed', rid=rid, evidence={
            'kind': 'recycle', 'account': self.engine.index.account(json.loads(self.engine.job(job_id)['config'])),
            'source_digest': self.source_digest(source), 'at': time.time()})

    async def verify_retired(self, source):
        # A restored or externally moved file must not be silently retired again.
        if await self.cloud.live_file_exists(source['id']):
            raise uncertain('已记录回收的文件仍存在或被恢复/移动，停止收尾；不会再次回收。')

    async def recycle_one(self, job_id, item_id, plan, source, scope, guard, journal='wash'):
        actions = plan[journal]['actions']
        previous = actions.get(source['id'])
        if previous:
            # Intent may have succeeded before a lost response/restart. Positive
            # recycle evidence is required; never replay an ambiguous deletion.
            await guard()
            if self.source_digest(previous.get('source', {})) != self.source_digest(source):
                raise uncertain('回收意图与当前文件身份不一致，停止收尾。')
            if previous['state'] == 'confirmed':
                evidence = previous.get('evidence')
                account = self.engine.index.account(json.loads(self.engine.job(job_id)['config']))
                if evidence:
                    if (evidence.get('account') != account or evidence.get('source_digest') != self.source_digest(source)
                            or evidence.get('kind') != 'recycle'):
                        raise uncertain('已保存的回收证据与账号或文件不一致。')
                elif not str(previous.get('rid', '')).isdigit():
                    raise uncertain('历史回收记录缺少可复用的核实证据。')
                await self.verify_retired(source)
                return
            rid = await self.recycled(source, previous)
            self.confirm_action(job_id, plan, source, rid, journal)
            self.save(item_id, plan, '回收结果已核实')
            return
        # 只保留「guard 之后」的那次核对：guard() 已经重新读过文件与目录，
        # 原先在它前面再 verify 一遍等于每个回收文件多 2 次云请求。
        # 回收前状态优先复用上一份刚核对过的清单（30 秒内），省掉一次全量翻页。
        before = await self.recycle_snapshot()
        if any(e.get('id') == source['id'] for e in before):
            raise uncertain('回收站已有相同文件 ID，禁止将其当作本次回收结果。')
        await guard()
        current = await self.verify(job_id, source, scope)
        if journal == 'wash': self.verify_duplicate_source(plan, current)
        self.engine.boundary(job_id)
        actions[source['id']] = {'state': 'intent', 'source': source, 'at': time.time(),
                                'before_rids': [e['rid'] for e in before]}
        self.save(item_id, plan, '正在回收旧版本')
        self.engine.audit(job_id, item_id, '移入回收站意图，文件 ID ' + source['id'] + '，规则 ' + plan[journal]['rule_version'])
        # No awaited work between durable intent and submitting this one mutation.
        # Readback still runs if UI pauses while the operation is in flight.
        try:
            await self.cloud.recycle(source['id'], source['parent_id'])
        except CloudError as exc:
            if fatal(exc):
                raise
        rid = await self.recycled(source, actions[source['id']])
        self.confirm_action(job_id, plan, source, rid, journal)
        self.save(item_id, plan, '回收结果已核实：' + source['id'])
        self.engine.audit(job_id, item_id, '已关联并核实回收，原文件 ID ' + source['id'] + '，回收记录 ID ' + rid)

    async def recycle_subtitle_conflicts(self, job_id, item_id, original, plan, config):
        conflicts = plan.get('subtitle_recycle', {}).get('sources', [])
        if not conflicts or config.get('repair_only'):
            return
        if plan.get('strm_filtered'):
            plan['warnings'].append('STRM 按规则跳过，冲突字幕保留在原处。')
            plan['subtitle_info'] = '字幕目标名称冲突，已选择一份归档。'
            return
        async def guard():
            actual = await self.cloud.file(original['id'])
            self.engine.check_source(original, actual, plan['target_name'], plan['dest'])
            self.verify_publication(original, plan)
            for sub in plan['subtitles']:
                current = await self.cloud.file(sub['source']['id'])
                self.engine.check_source(sub['source'], current, sub['target_name'], plan['dest'])
        for source in conflicts:
            await self.recycle_one(job_id, item_id, plan, source, config['source_cids'], guard, 'subtitle_recycle')

    async def discard(self, job_id, item_id, original, plan, config):
        winner = plan['wash']['winner']['source']
        duplicate = plan['wash'].get('reason') == 'identical_copy'
        legacy_merge = duplicate and not plan['wash']['duplicate'].get('discard_bound_subtitles')
        if legacy_merge:
            await self.sync_duplicate_subtitles(job_id, item_id, plan, config)
        async def guard():
            current = await self.verify(job_id, winner, [config['archive_cid']])
            self.verify_duplicate_source(plan, current)
            if legacy_merge:
                for sub in plan['wash']['duplicate'].get('subtitles', []):
                    await self.verify_duplicate_subtitle(job_id, sub, config)
        sources = [original] if legacy_merge else [original, *original.get('subtitles', [])]
        for source in sources:
            await self.recycle_one(job_id, item_id, plan, source, config['source_cids'], guard)
        reason = plan['wash'].get('reason')
        step = ('完全相同的新副本已入回收站' if duplicate else
                '比较信息不足，按策略保留旧版' if reason == 'unknown_fallback' else
                '规则同分，按策略保留旧版' if reason == 'equal_rules' else '劣质新版本已入回收站')
        message = ('同片同集、SHA1 与精确字节数一致；归档原版已重新核实并保留。' if duplicate else
                   '已核实保留的旧版本；新版本已逐文件核实回收结果。')
        if duplicate and plan['wash']['duplicate'].get('subtitles'):
            subs = plan['wash']['duplicate']['subtitles']
            message += f'字幕已归并并同步 {len(subs)} 项；其中回收重复字幕 {sum(s["action"] == "recycle" for s in subs)} 项。'
        self.engine.update_item(item_id, plan=plan, state='recycled', step=step,
                                message=message + '；'.join(plan.get('warnings', [])))

    async def discard_many(self, job_id, entries, config):
        """Batch discard only verified, subtitle-free incoming videos.

        Recheck both sides and directory scopes using shared listings. Persist
        every exact recycle intent before one request; ambiguous responses are
        left to the existing per-item recovery without resubmission.
        """
        by_parent = {}
        for item_id, original, plan in entries:
            wash = plan.get('wash') or {}
            duplicate = wash.get('duplicate') or {}
            if (original.get('subtitles') or duplicate.get('subtitles') or wash.get('actions')
                    or not (wash.get('winner') or {}).get('source')):
                continue
            by_parent.setdefault(str(original['parent_id']), []).append((item_id, original, plan))
        done = 0
        for parent, group in by_parent.items():
            if len(group) < 2 or len({str(original['id']) for _, original, _ in group}) != len(group):
                continue
            self.engine.boundary(job_id)
            scope = await self.cloud.directory(parent)
            if not {str(x) for x in config['source_cids']} & {str(x) for x in scope}:
                raise uncertain('待淘汰副本已离开来源目录，停止批量回收。')
            source_listing = {str(e['id']): e for e in await self.cloud.children(parent) if not e.get('is_dir')}
            winner_listings = {}
            for item_id, original, plan in group:
                current = source_listing.get(str(original['id']))
                if not current:
                    raise uncertain('待淘汰副本已不存在，逐项核查。')
                self.engine.check_source(original, current, original['name'], parent)
                self.verify_duplicate_source(plan, current)
                winner = plan['wash']['winner']['source']
                wp = str(winner['parent_id'])
                if wp not in winner_listings:
                    trail = await self.cloud.directory(wp)
                    if str(config['archive_cid']) not in {str(x) for x in trail}:
                        raise uncertain('保留版本已离开归档范围，停止批量回收。')
                    winner_listings[wp] = {str(e['id']): e for e in await self.cloud.children(wp) if not e.get('is_dir')}
                current_winner = winner_listings[wp].get(str(winner['id']))
                if not current_winner:
                    raise uncertain('保留的旧版本已不存在，停止批量回收。')
                self.engine.check_source(winner, current_winner, winner['name'], wp)
                self.verify_duplicate_source(plan, current_winner)
            before = await self.recycle_snapshot()
            ids = {original['id'] for _, original, _ in group}
            if any(e.get('id') in ids for e in before):
                raise uncertain('回收站已有相同文件 ID，停止批量回收。')
            before_rids = [e['rid'] for e in before if e.get('rid')]
            for item_id, original, plan in group:
                plan['wash'].setdefault('actions', {})[original['id']] = {
                    'state': 'intent', 'source': original, 'at': time.time(), 'before_rids': before_rids}
                self.save(item_id, plan, '已记录批量淘汰意图')
            self.engine.boundary(job_id)
            self.engine.store.event('organize', f'任务 {job_id}：已核实保留版本，批量回收 {len(group)} 个新副本。')
            try:
                await self.cloud.recycle_many([original['id'] for _, original, _ in group], parent)
            except CloudError as exc:
                if fatal(exc):
                    raise
                return done  # Intents are reconciled by discard(), never replayed.
            after = await self.cloud.recycle_files()
            for item_id, original, plan in group:
                record = match_record(original, plan['wash']['actions'][original['id']], after)
                if not record:
                    continue  # Individual recovery can tolerate delayed recycle visibility.
                await self.verify_retired(original)
                self.confirm_action(job_id, plan, original, record['rid'])
                reason = plan['wash'].get('reason')
                step = ('完全相同的新副本已入回收站' if reason == 'identical_copy' else
                        '比较信息不足，按策略保留旧版' if reason == 'unknown_fallback' else
                        '规则同分，按策略保留旧版' if reason == 'equal_rules' else '劣质新版本已入回收站')
                self.engine.update_item(item_id, plan=plan, state='recycled', step=step,
                    message='保留版本和新副本已按目录核实；批量回收结果已确认。' + '；'.join(plan.get('warnings', [])))
                done += 1
        return done

    def verify_duplicate_source(self, plan, current):
        proof = plan['wash'].get('duplicate')
        if not proof:
            return
        if proof.get('discard_bound_subtitles') and current['id'] in proof.get('subtitle_ids', []):
            return
        expected = (proof['sha1'], proof['size'])
        for sub in proof.get('subtitles', []):
            if current['id'] in (sub['source']['id'], sub['keeper']['id']):
                expected = (sub['sha1'], sub['size'])
                break
        if self.content_key(current) != expected:
            raise uncertain('重复副本的内容指纹或精确大小已缺失/变化，停止回收。')

    async def repair(self, job_id, item_id, original, plan, config):
        # The existing repair-output button has a read-only cloud contract. It
        # must never finish a cancelled replacement's pending recycle/rename.
        actual = await self.cloud.file(original['id'])
        if actual['name'] not in (plan['wash']['stage_name'], plan['target_name']):
            raise uncertain('补齐输出时新文件名称不在已记录范围内。')
        await self.verify(job_id, {**original, 'name': actual['name'], 'parent_id': plan['dest']}, [config['archive_cid']])
        for sub in plan['subtitles']:
            current = await self.cloud.file(sub['source']['id'])
            if current['name'] not in (sub['stage_name'], sub['target_name']):
                raise uncertain('补齐输出时字幕名称不在已记录范围内。')
            await self.verify(job_id, {**sub['source'], 'name': current['name'], 'parent_id': plan['dest']}, [config['archive_cid']])
            sub['export_cloud_name'] = current['name']
        await self.engine.export(job_id, item_id, original, actual, plan)
        for sub in plan['subtitles']:
            sub.pop('export_cloud_name', None)
        if not plan['wash'].get('finalized'):
            plan['warnings'].append('仅补齐本地输出；已取消洗版的回收或最终命名未继续，请人工核查。')
        self.engine.index.record_managed(job_id, original, actual, plan)
        self.engine.update_item(item_id, plan=plan, state='done', step='已补齐本地输出，未修改云端', message='；'.join(plan['warnings']))

    async def recycle_published_batch(self, job_id, entries, config):
        """Batch old files only after every new playback has been verified.

        Persist each exact intent before the single submission. A lost response
        is reconciled through recycle_one; it is never blindly resubmitted.
        """
        if not hasattr(self.cloud, 'recycle_many') or len(entries) < 2:
            return
        groups = {}
        for item_id, original, plan in entries:
            wash = plan.get('wash', {})
            if not wash.get('published') or wash.get('actions'):
                continue
            for old in wash.get('old', []):
                for source in [old['source'], *old.get('subtitles', [])]:
                    groups.setdefault(str(source['parent_id']), []).append((item_id, original, plan, source))
        for parent, group in groups.items():
            if len(group) < 2 or len({str(source['id']) for _, _, _, source in group}) != len(group):
                continue
            # Shared directories are read once; check exact identity and scope.
            self.engine.boundary(job_id)
            trail = await self.cloud.directory(parent)
            if str(config['archive_cid']) not in {str(x) for x in trail}:
                raise uncertain('旧版目录已离开归档范围，停止批量回收。')
            listing = {str(e['id']): e for e in await self.cloud.children(parent)}
            for item_id, original, plan, source in group:
                current = listing.get(str(source['id']))
                if not current:
                    raise uncertain('批量回收前旧版已不存在，停止并逐项核查。')
                self.engine.check_source(source, current, source['name'], parent)
                new = listing.get(str(original['id'])) if str(plan['dest']) == parent else None
                if not new:
                    new = await self.cloud.file(original['id'])
                self.engine.check_source(original, new, plan['wash']['stage_name'], plan['dest'])
                self.verify_publication(original, plan)
            before = await self.recycle_snapshot()
            if any(e.get('id') in {source['id'] for _, _, _, source in group} for e in before):
                raise uncertain('回收站已有相同文件 ID，停止批量回收。')
            before_rids = [e['rid'] for e in before if e.get('rid')]
            for item_id, original, plan, source in group:
                plan['wash']['actions'][source['id']] = {'state': 'intent', 'source': source,
                    'at': time.time(), 'before_rids': before_rids}
                self.save(item_id, plan, '已记录批量回收意图')
            self.engine.boundary(job_id)
            self.engine.store.event('organize', f'任务 {job_id}：新版输出均已核实，批量回收旧版 {len(group)} 个文件。')
            try:
                await self.cloud.recycle_many([source['id'] for _, _, _, source in group], parent)
            except CloudError as exc:
                if fatal(exc):
                    raise
                # Individual recovery uses durable intents and never replays.
                return
            after = await self.cloud.recycle_files()
            for item_id, original, plan, source in group:
                action = plan['wash']['actions'][source['id']]
                record = match_record(source, action, after)
                if record:
                    await self.verify_retired(source)
                    self.confirm_action(job_id, plan, source, record['rid'])
                    self.save(item_id, plan, '批量回收结果已核实')

    async def replace(self, job_id, item_id, original, plan, config, stage_only=False, publish_only=False):
        wash, dest = plan['wash'], plan['dest']
        async def new_guard():
            self.engine.boundary(job_id)
            actual = await self.cloud.file(original['id'])
            if actual['name'] not in (wash['stage_name'], plan['target_name']):
                raise uncertain('新版本名称已发生外部变化，停止回收旧版。')
            await self.verify(job_id, {**original, 'name': actual['name'], 'parent_id': dest}, [config['archive_cid']])
            self.verify_publication(original, plan)
        self.engine.boundary(job_id)
        actual = await self.cloud.file(original['id'])
        if actual['name'] == plan['target_name'] and actual['parent_id'] == dest and wash.get('finalizing'):
            self.engine.check_source(original, actual, plan['target_name'], dest)
        else:
            actual = await self.engine.change_file(job_id, item_id, original, wash['stage_name'], dest, config['retries'])
        for sub in plan['subtitles']:
            source = sub['source']
            stage = sub['stage_name']
            current = await self.cloud.file(source['id'])
            if current['name'] == sub['target_name'] and current['parent_id'] == dest and wash.get('finalizing'):
                self.engine.check_source(source, current, sub['target_name'], dest)
            else:
                await self.engine.change_file(job_id, item_id, source, stage, dest, config['retries'])
        self.engine.update_item(item_id, state='archived', step='新版本临时归档已核实')
        if stage_only and not wash.get('published'):
            if 'ready_at' not in wash:
                wash['ready_at'] = time.time() + config.get('strm_delay', 0)
            self.engine.update_item(item_id, plan=plan, state='archived', step='洗版已归档，等待目录增量同步')
            return
        if not wash.get('published'):
            plan['warnings'] = [w for w in plan['warnings'] if not w.startswith('外挂字幕下载失败')]
            if 'ready_at' not in wash:
                wash['ready_at'] = time.time() + config.get('strm_delay', 0)
                self.save(item_id, plan, '等待发布新版本')
            await asyncio.sleep(max(0, wash['ready_at'] - time.time()))
            self.engine.boundary(job_id)
            await self.engine.export(job_id, item_id, original, actual, plan)
            self.save(item_id, plan, '新版本输出已写入')
            wash['published'] = True
            self.save(item_id, plan, '新版本 STRM 已就绪')
        # Persisted flag alone is not proof that a user has not removed output.
        self.verify_publication(original, plan)
        if publish_only:
            return
        for old in wash['old']:
            for source in [old['source'], *old['subtitles']]:
                await self.recycle_one(job_id, item_id, plan, source, [config['archive_cid']], new_guard)
            self.cleanup_old(job_id, item_id, old, plan)
            self.save(item_id, plan, '旧版本本地映射已处理')
        self.engine.boundary(job_id)
        wash['finalizing'] = True
        self.save(item_id, plan, '正在改成最终名称')
        stage = {**original, 'name': wash['stage_name'], 'parent_id': dest}
        actual = await self.engine.change_file(job_id, item_id, stage, plan['target_name'], dest, config['retries'])
        for sub in plan['subtitles']:
            await self.engine.change_file(job_id, item_id, {**sub['source'], 'name': sub['stage_name'], 'parent_id': dest}, sub['target_name'], dest, config['retries'])
        wash['finalized'] = True
        self.save(item_id, plan, '新版本最终名称已核实')
        self.engine.index.record_managed(job_id, original, actual, plan)
        await self.recycle_subtitle_conflicts(job_id, item_id, original, plan, config)
        message = '；'.join(filter(None, [wash_reason(wash),
                                          '新版本就绪后，旧版本回收记录已关联核实',
                                          plan.get('subtitle_info'), *plan['warnings']]))
        self.engine.update_item(item_id, plan=plan, state='done', step='洗版与输出完成', message=message)

    def verify_publication(self, original, plan):
        row = self.store.db.execute('SELECT * FROM playbacks WHERE id=?', (plan['playback_id'],)).fetchone()
        if not row or row['revoked'] or row['file_id'] != original['id']:
            raise uncertain('新版本播放入口不可用，停止回收旧版。')
        if plan['relative_path'] not in plan.get('local_files', {}):
            raise uncertain('新版本尚未记录完整 STRM 输出，停止回收旧版。')
        for relative, expected in plan['local_files'].items():
            path = self.local_path(relative)
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise uncertain('新版本本地输出缺失或已改变，停止回收旧版。')

    def cleanup_old(self, job_id, item_id, old, plan):
        self.engine.boundary(job_id)
        for relative, expected in old['local_files'].items():
            if relative in plan.get('local_files', {}):
                continue
            path = self.local_path(relative)
            if path.suffix.lower() not in SUBTITLES | {'.strm'}:
                raise uncertain('旧输出清单包含不允许清理的文件类型。')
            if path.is_file():
                if path.stat().st_size > 20 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    warning = '旧本地文件已被修改，保留供人工核查：' + relative
                    if warning not in plan['warnings']:
                        plan['warnings'].append(warning)
                    continue
                self.engine.audit(job_id, 0, '清理已确认旧输出意图：' + relative)
                self.engine.outputs.unlink_recorded(job_id,relative,path,item_id)
                # 文件已按登记字节删除，登记本身也要撤掉；留着会挡住同名的新版本。
                with self.store.db:
                    self.store.db.execute('DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?',
                                          (relative, self.engine.index.account(json.loads(self.engine.job(job_id)['config'])),
                                           old['source']['id']))
        with self.store.db:
            self.store.db.execute('UPDATE playbacks SET revoked=1 WHERE file_id=?', (old['source']['id'],))
            self.store.db.execute('DELETE FROM organized_files WHERE file_id=?', (old['source']['id'],))
            self.store.db.execute('UPDATE output_registry SET active=0 WHERE file_id=? AND account_id=?', (old['source']['id'], self.engine.index.account(json.loads(self.engine.job(job_id)['config']))))
        self.engine.index.remove_managed(job_id, old['source']['id'])
