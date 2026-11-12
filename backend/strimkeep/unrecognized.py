"""Divert remaining unrecognized packages without flattening or renaming them."""
import json
import re
from pathlib import Path
from .media import KNOWN_VIDEOS, SEASON_DIR, folder_context, parse_name, normal, NeedsReview
from .cloud115 import CloudError, fatal


class Unrecognized:
    def __init__(self, engine, uncertain_state):
        self.engine = engine
        self.preserve_errors = (CloudError, uncertain_state)

    async def flush(self, job_id, config):
        engine, cloud = self.engine, self.engine.cloud
        rows = engine.store.db.execute('SELECT * FROM organize_items WHERE job_id=? ORDER BY id', (job_id,)).fetchall()
        groups, trails, listings = {}, {}, {}
        fresh_children = getattr(cloud, 'fresh_children', cloud.children)

        async def trail(cid):
            if cid not in trails:
                trails[cid] = await cloud.directory(cid)
            return trails[cid]

        async def listing(cid):
            if cid not in listings:
                listings[cid] = await cloud.children(cid)
            return listings[cid]

        def finish(members, state, step, reason=None):
            for row, original, plan in members:
                message = plan['reason']
                if reason and reason not in message:
                    message += '；' + reason
                engine.update_item(row['id'], state=state, plan=plan, step=step, message=message)
                icon = '✅' if state == 'pending' else '⚠️'
                engine.store.event('organize',
                                   f'{icon} [未识别] {step}：{original.get("name", "")[:70]}'
                                   + (f' | {reason[:60]}' if reason else ''))
                if state == 'skipped':
                    engine.audit(job_id, row['id'], message)

        def work_directory(name, original):
            # Do not confuse a broad category with a movie package after its
            # successfully organised siblings and subdirectories disappear.
            context = folder_context(name)
            if not context:
                return False
            title = normal(context.get('title') or '').casefold()
            category = re.sub(r'[\s._-]+', '', title)
            if re.fullmatch(r'.{0,8}(?:电影|影片|剧集|电视剧|影视|动漫|综艺|纪录片|movie|movies|films|tv|series)(?:分类|合集|资源|目录|列表|库)?', category):
                return False
            if re.fullmatch(r'.{0,8}(?:资源|分类|合集|媒体库|影视库|影片库|待整理|下载|collections)', category):
                return False
            # An explicit year or TMDB identity identifies a work. Otherwise
            # require actual agreement with the video title, rather than
            # interpreting an arbitrary release/category label as a work.
            if context.get('year') or context.get('tmdb_id'):
                return True
            try:
                hint = parse_name(original['name'])
            except NeedsReview:
                return False
            query = normal(hint.get('query') or '').casefold()
            return len(title) >= 3 and len(query) >= 3 and (title in query or query in title)

        async def move_individual(members):
            for row, original, plan in members:
                try:
                    # Persist the individual decision before any cloud mutation;
                    # a resumed task must not reinterpret it as a directory move.
                    plan.pop('source_bundle', None)
                    plan['quarantine_file'] = True
                    engine.update_item(row['id'], plan=plan, step='逐个转移未识别文件')
                    sources = [original, *original.get('subtitles', [])]
                    target = await fresh_children(plan['dest'])
                    if any(e['name'] == source['name'] and e['id'] != source['id'] for e in target for source in sources):
                        raise CloudError('未识别目录有同名文件，原位保留。', 'NAME_CONFLICT', 409)
                    for source in sources:
                        await engine.change_file(job_id, row['id'], source, source['name'], plan['dest'], 0)
                    finish([(row, original, plan)], 'pending', '未识别文件已逐个转移')
                except self.preserve_errors as exc:
                    if isinstance(exc, CloudError) and exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                        raise
                    finish([(row, original, plan)], 'skipped', '未识别文件转移未完成，保留记录', str(exc))

        # Decide using the full original batch: successful or skipped siblings
        # mean this is a partial failure, not an intact failed package.
        for row in rows:
            plan = json.loads(row['plan'])
            if not plan.get('pending') or row['state'] in ('pending', 'skipped'):
                continue
            original = json.loads(row['original'])
            try:
                bundle = plan.get('source_bundle')
                # Keep an already-submitted intent stable across restart; an
                # unsent legacy broad bundle is re-evaluated at the leaf.
                if not bundle or not plan.get('quarantine_sent'):
                    chain = await trail(original['parent_id'])
                    roots = [cid for cid in config['source_cids'] if cid in chain]
                    if not roots:
                        raise CloudError('原目录已不在本次待整理范围，保留现场。', 'DIRECTORY_CHANGED', 409)
                    root = max(roots, key=chain.index)
                    bundle = None
                    if chain[-1] != root and not plan.get('quarantine_file'):
                        parent, candidate = chain[-2], chain[-1]
                        matches = [e for e in await listing(parent) if e['id'] == candidate and e['is_dir']]
                        if len(matches) != 1:
                            raise CloudError('无法核实原资源目录，保留现场。', 'DIRECTORY_CHANGED', 409)
                        work = matches[0]
                        # Season directories carry no title. Preserve the actual
                        # enclosing work folder, but never the selected root.
                        if SEASON_DIR.fullmatch(work['name']) and parent != root:
                            work_matches = [e for e in await listing(chain[-3]) if e['id'] == parent and e['is_dir']]
                            work = work_matches[0] if len(work_matches) == 1 else None
                        if work and work_directory(work['name'], original):
                            bundle = {k: work[k] for k in ('id', 'name', 'parent_id')}
                            plan['source_bundle'] = bundle
                    if not bundle:
                        plan.pop('source_bundle', None)
                key = ('folder', bundle['id']) if bundle else ('file', original['id'])
                groups.setdefault(key, []).append((row, original, plan))
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    raise
                finish([(row, original, plan)], 'skipped', '未识别资源原位保留', str(exc))

        for (kind, ident), members in groups.items():
            engine.boundary(job_id)
            try:
                if kind == 'file':
                    await move_individual(members)
                    continue
                else:
                    bundle = members[0][2]['source_bundle']
                    # Every video originally scanned under the candidate must
                    # belong to this failed group, including already moved files.
                    group_rows = {row['id'] for row, _, _ in members}
                    partial = False
                    for other in rows:
                        if other['id'] in group_rows:
                            continue
                        parent = json.loads(other['original'])['parent_id']
                        if ident in await trail(parent):
                            partial = True
                            break
                    if partial and not any(plan.get('quarantine_sent') for _, _, plan in members):
                        await move_individual(members)
                        continue
                    dest = members[0][2]['dest']
                    target = await fresh_children(dest)
                    present = [e for e in target if e['id'] == ident]
                    if present:
                        if len(present) != 1 or present[0]['name'] != bundle['name'] or not present[0]['is_dir']:
                            raise CloudError('转移后的目录身份不一致，保留现场。', 'DIRECTORY_CHANGED', 409)
                    else:
                        if any(e['name'] == bundle['name'] for e in target):
                            raise CloudError('未识别目录有同名目录，不合并、不改名，原位保留。', 'NAME_CONFLICT', 409)
                        source = [e for e in await fresh_children(bundle['parent_id']) if e['id'] == ident]
                        if len(source) != 1 or source[0]['name'] != bundle['name'] or not source[0]['is_dir']:
                            raise CloudError('原资源目录名称或位置变化，保留现场。', 'DIRECTORY_CHANGED', 409)
                        member_ids = {str(original['id']) for _, original, _ in members}
                        member_parents = {str(original['parent_id']) for _, original, _ in members}
                        attached_ids = {str(sub['id']) for _, original, _ in members for sub in original.get('subtitles', [])}
                        async def checked_tree():
                            # Only work/Season N nesting is accepted; unrelated
                            # subfolders (including empty ones) stay behind.
                            files, seen = [], {str(ident)}
                            for entry in await fresh_children(ident):
                                if not entry['is_dir']:
                                    files.append(entry)
                                elif SEASON_DIR.fullmatch(entry['name']) and str(entry['id']) not in seen:
                                    # Empty/previously organised seasons are not
                                    # part of this failed package.
                                    if str(entry['id']) not in member_parents:
                                        return None
                                    seen.add(str(entry['id']))
                                    children = await fresh_children(entry['id'])
                                    if any(child['is_dir'] for child in children):
                                        return None
                                    files.extend(children)
                                else:
                                    return None
                            if any(Path(e['name']).suffix.lower() in KNOWN_VIDEOS and str(e['id']) not in member_ids for e in files):
                                return None
                            # Unknown non-video payloads or subtitles belonging to
                            # something else cannot hitch a ride with the package.
                            safe_metadata = {'.nfo', '.jpg', '.jpeg', '.png', '.webp'}
                            if any(str(e['id']) not in member_ids | attached_ids and Path(e['name']).suffix.lower() not in safe_metadata for e in files):
                                return None
                            return files
                        contents = await checked_tree()
                        if contents is None:
                            await move_individual(members)
                            continue
                        for row, original, plan in members:
                            current = await cloud.file(original['id'])
                            if any(current.get(k) != original.get(k) for k in ('id', 'name', 'parent_id', 'size')):
                                raise CloudError('原视频名称、位置或大小变化，保留原目录。', 'FILE_CHANGED', 409)
                        latest = await checked_tree()
                        if latest is None:
                            raise CloudError('目录内容在核查期间发生变化，未识别资源原位保留。', 'DIRECTORY_CHANGED', 409)
                        for _, original, _ in members:
                            matches = [e for e in latest if str(e['id']) == str(original['id']) and not e['is_dir']]
                            if len(matches) != 1 or any(matches[0].get(k) != original.get(k) for k in ('id', 'name', 'parent_id', 'size')):
                                raise CloudError('原视频在核查期间发生变化，未识别资源原位保留。', 'FILE_CHANGED', 409)
                        if any(plan.get('quarantine_sent') for _, _, plan in members):
                            raise CloudError('已有整目录移动意图，结果未确认；不重复移动。', 'DIRECTORY_CHANGED', 409)
                        for row, original, plan in members:
                            plan['quarantine_sent'] = True
                            engine.update_item(row['id'], plan=plan, step='正在把整个目录移入未识别')
                        engine.audit(job_id, members[0][0]['id'], '原目录整体移动：' + ident + ' / ' + bundle['name'])
                        engine.boundary(job_id)
                        try:
                            await cloud.move(ident, dest)
                        except CloudError as exc:
                            if fatal(exc):
                                raise
                        checked = [e for e in await fresh_children(dest) if e['id'] == ident]
                        if len(checked) != 1 or checked[0]['name'] != bundle['name'] or not checked[0]['is_dir']:
                            raise CloudError('整目录移动结果未确认，保留记录，不拆分或重复移动。', 'DIRECTORY_CHANGED', 409)
                finish(members, 'pending', '已原样转入未识别目录，保留目录结构')
            except self.preserve_errors as exc:
                if isinstance(exc, CloudError) and exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    raise
                finish(members, 'skipped', '未识别转移未完成，保留目录与记录', str(exc))
