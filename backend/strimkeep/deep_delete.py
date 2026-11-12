# -*- coding: utf-8 -*-
"""Emby 深度删除联动：神医助手 deep.delete / Emby 原生 library.deleted 事件处理。

事件进来后只做三件事：解析 → 对齐台账 → **写进队列**，立刻返回（不再让 Emby 等我们删完）。
真正的删除由后台 worker 按序执行，每条都回读确认，进度写进面板。

安全边界（全部满足才会动网盘）：
- 令牌鉴权；事件类型只认 Movie/Episode/Season/Series（或带 tmdb 号的扫描通知）
- 路径按「严格上下级 / tmdb 号」对齐台账，目录事件必须有至少一条台账证据
- 网盘文件只进 115 回收站（可还原），绝不彻底删除
- dry_run（预演）模式只记录将要做什么
"""
import asyncio
import json
import re
import time
from pathlib import Path

TYPES = ('Movie', 'Episode', 'Season', 'Series')


def parse_event(payload):
    """宽容解析删除事件；不认识的形态返回 None（宁可不删）。

    Emby 有两种删除通知：
    - 你手动删条目：带完整 `Item.Path` 与 `Type`，按路径精确对齐；
    - 扫描判定「条目已被移除」可能只给 TMDB 编号；保留事件供上层提示，
      但缺少路径时无法区分单集、整季与整部，绝不能据此自动删除。
    """
    if not isinstance(payload, dict):
        return None
    event = str(payload.get('Event') or payload.get('event') or payload.get('event_name') or '')
    if 'delete' not in event.lower():
        return None
    item = payload.get('Item') if isinstance(payload.get('Item'), dict) else payload
    typ = str(item.get('Type') or item.get('ItemType') or item.get('item_type') or '')
    path = str(item.get('Path') or item.get('path') or payload.get('Path') or payload.get('path') or '').strip()
    providers = item.get('ProviderIds') if isinstance(item.get('ProviderIds'), dict) else {}
    tmdb = str(providers.get('Tmdb') or item.get('Tmdb') or item.get('tmdb') or '').strip()
    name = str(item.get('Name') or item.get('name') or '')
    if not path:
        if not (tmdb.isdigit() and tmdb):
            return None
        return {'type': typ or 'unknown', 'path': '', 'name': name,
                'tmdb': tmdb, 'event': event or 'delete'}
    if typ not in TYPES:
        return None
    return {'type': typ, 'path': path.rstrip('/'), 'name': name, 'tmdb': tmdb,
            'event': event or 'delete'}


def _relative_candidates(store, path):
    """事件路径 → 可能的「库内相对路径」候选。

    Emby 事件里的 Path 一定带库的挂载前缀（本项目默认 `/media`），而台账里存的是
    库内相对路径。优先按配置的 `console_config.emby_strm_path` 剥前缀；没配置或
    对不上时，再退一步去掉第一段（当作挂载点）、以及原样三种候选。
    """
    normalized = '/' + str(path).strip('/')
    mapping = store.get('console_config', {}) or {}
    prefix = str(mapping.get('emby_strm_path') or '').rstrip('/')
    candidates = []
    if prefix and (normalized == prefix or normalized.startswith(prefix + '/')):
        candidates.append(normalized[len(prefix):].strip('/'))
    parts = [part for part in normalized.split('/') if part]
    if len(parts) > 1:
        candidates.append('/'.join(parts[1:]))
    candidates.append('/'.join(parts))
    unique = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _hits(rel, event_path, is_dir, candidates):
    """台账条目是否受这次事件影响。

    目录事件只认「事件路径是条目的上级目录」这种严格关系：不能用末段目录名去
    模糊匹配——`Season 1` 这种通用目录名会把别的剧一起删掉。文件事件保留后缀
    对齐，兼容 神医助手 直接发网盘路径的情况。
    """
    rel_norm = rel.strip('/')
    target = '/' + event_path.strip('/')
    for candidate in candidates:
        if is_dir:
            if rel_norm == candidate or rel_norm.startswith(candidate + '/'):
                return True
        elif rel_norm == candidate or target.endswith('/' + rel_norm):
            return True
    return False


def match_targets(store, event):
    """把事件路径对齐到台账：整理/同步产出（managed）与分享 STRM（share）。"""
    # A TMDB id identifies a work, not the deleted episode or season.
    # Never widen a path-bearing event to every item in the work, and do
    # not mutate anything when a webhook has no path evidence.
    path = str(event.get('path') or '').replace('\\', '/')
    if not path:
        return {'managed': [], 'share': [], 'type': event.get('type') or 'unknown',
                'path': '', 'tmdb': str(event.get('tmdb') or '')}
    kind = event.get('type')
    is_dir = kind in ('Season', 'Series')
    if is_dir:
        parts = [part for part in path.strip('/').split('/') if part]
        marker = re.compile(r'(?i)\{tmdb-\d+\}')
        # A generic media root or "Season 1" is not an individual work.
        # Directory events must identify the specific work and, for a season,
        # the specific season within it.
        if kind == 'Season':
            safe = (len(parts) >= 2 and re.fullmatch(r'(?i)season\s*0*\d+', parts[-1])
                    and marker.search(parts[-2]))
        else:
            safe = bool(parts and marker.search(parts[-1]))
        if not safe:
            return {'managed': [], 'share': [], 'type': kind, 'path': path}
    candidates = _relative_candidates(store, path)
    managed, share = [], []
    for row in store.db.execute('SELECT file_id, playback_id, relative_path FROM organized_files'):
        if _hits(row['relative_path'], path, is_dir, candidates):
            managed.append(dict(row))
    for row in store.db.execute('SELECT id, strm_path FROM share_strms WHERE revoked=0'):
        if _hits(row['strm_path'], path, is_dir, candidates):
            share.append(dict(row))
    return {'managed': managed, 'share': share, 'type': event['type'], 'path': path}


def drop_internal(store, targets):
    """剔除「本地文件是程序自己删的」目标（0.8.32）。

    Emby 的「条目被删」通知分不清是谁删的：用户在 Emby 里删条目要删云端，但程序清理
    旧命名副本/退役输出时 Emby 也会发同一条通知——那不该删云端。这里用删除登记过滤。
    返回 (过滤后的 targets, 跳过条数)。
    """
    from .internal_delete import recent
    seen = recent(store)
    if not seen:
        return targets, 0
    managed = [t for t in targets['managed'] if (t.get('relative_path') or '') not in seen]
    share = [t for t in targets['share'] if (t.get('strm_path') or '') not in seen]
    dropped = (len(targets['managed']) - len(managed)) + (len(targets['share']) - len(share))
    return {'managed': managed, 'share': share}, dropped


def selftest(store, item_type_movie='Movie', item_type_episode='Episode'):
    """只读自检：用台账里的真实路径跑一遍匹配，用来确认"接线对不对"。

    不写任何东西、不看开关、永远不删——面板上的「链路自检」按钮就是调它，
    这样即使 Emby 那头还没配好，也能立刻知道我们这侧的匹配逻辑是否正常。
    """
    mapping = store.get('console_config', {}) or {}
    prefix = str(mapping.get('emby_strm_path') or '/strimkeep').rstrip('/')
    cases = []

    def run(label, path, item_type):
        targets = match_targets(store, {'type': item_type, 'path': path})
        cases.append({'case': label, 'path': path,
                      'managed': len(targets['managed']), 'share': len(targets['share'])})

    managed = store.db.execute(
        'SELECT relative_path FROM organized_files ORDER BY rowid DESC LIMIT 1').fetchone()
    share = store.db.execute(
        'SELECT strm_path FROM share_strms WHERE revoked=0 ORDER BY rowid DESC LIMIT 1').fetchone()
    if managed and managed['relative_path']:
        run('整理产物 · 单集', prefix + '/' + managed['relative_path'].lstrip('/'), item_type_episode)
    if share and share['strm_path']:
        run('分享 STRM · 单集', prefix + '/' + share['strm_path'].lstrip('/'), item_type_movie)
    # 反例：通用目录名不允许命中，命中说明匹配逻辑退化了。
    generic = match_targets(store, {'type': 'Season', 'path': prefix + '/Season 1'})
    return {'emby_prefix': prefix, 'cases': cases,
            'generic_directory_matched': len(generic['managed']) + len(generic['share'])}

async def prune_cloud_empty_chain(engine, cid, notes):
    """删完源文件后，自底向上回收 115 上变空的目录（同步根/归档根及更上层永不动）。

    每层只需 `children` 非空即停；`recycle_empty_directory` 自身还会再核位置与空目录，
    所以这里不做任何猜测，失败只记 note，不影响已确认的源文件删除结论。
    """
    from .cloud115 import CloudError
    cloud = engine.cloud
    if not hasattr(cloud, 'recycle_empty_directory'):
        return
    roots = set()
    for key in ('sync_config', 'organize_config'):
        try:
            config = engine.store.get(key) or {}
        except Exception:
            continue
        if not isinstance(config, dict):
            continue
        roots.update(str(x) for x in (config.get('source_cids') or []) if x)
        if config.get('archive_cid'):
            roots.add(str(config['archive_cid']))
    seen = set()
    while cid and str(cid) not in roots and str(cid) not in seen:
        seen.add(str(cid))
        try:
            if await cloud.children(str(cid)):
                return                       # 还有其它内容：整目录保留
            trail = await cloud.directory(str(cid))
            if len(trail) < 2 or str(trail[-1]) != str(cid):
                return
            if not roots.intersection(str(x) for x in trail[:-1]):
                return                       # 不在同步/归档树之下：不碰
            await cloud.recycle_empty_directory(str(cid), str(trail[-2]))
            notes.append('已回收网盘空目录 %s' % cid)
            cid = str(trail[-2])
        except CloudError as exc:
            notes.append('网盘空目录回收未完成（%s）' % (exc.code or 'UNKNOWN'))
            return
        except Exception:
            return


def _prepare_cloud(engine, target):
    """逐条核验：受管身份、本地 STRM、网盘来源。返回 (info, status, message)。"""
    import hashlib
    from pathlib import Path
    try:
        store = engine.store
        account = engine.index.account({})
        fid, pid, relative = target['file_id'], target['playback_id'], target['relative_path']
        old = store.db.execute('SELECT * FROM output_registry WHERE account_id=? AND file_id=? AND active=1', (account, fid)).fetchone()
        owner = store.db.execute('SELECT * FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
        if not old or not owner or old['playback_id'] != pid or old['relative_path'] != relative:
            return None, 'skipped', '受管文件或播放身份无法核实，已保留。'
        files, source = json.loads(old['files']), json.loads(old['source'])
        expected = files.get(relative) if isinstance(files, dict) else None
        if not expected or (owner['account_id'], owner['file_id'], owner['digest']) != (account, fid, expected):
            return None, 'skipped', '本地输出归属无法核实，已保留。'
        path, root = engine.root / relative, engine.root.resolve()
        if (Path(relative).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root)
                or any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root))):
            return None, 'skipped', '本地路径不安全，已保留。'

        def local_ok():
            if not path.exists():
                return True
            return (path.is_file() and path.stat().st_size <= 20 * 1024 * 1024
                    and hashlib.sha256(path.read_bytes()).hexdigest() == expected)

        if not local_ok():
            return None, 'skipped', '本地 STRM 内容已变化，已保留。'
        if (not isinstance(source, dict) or str(source.get('id')) != str(fid)
                or not source.get('name') or not str(source.get('parent_id') or '').isdigit()
                or type(source.get('size')) is not int or source['size'] <= 0
                or not re.fullmatch(r'[0-9a-fA-F]{40}', str(source.get('sha1') or ''))):
            return None, 'skipped', '网盘来源身份不完整，已保留。'
        intent_key = 'deep-delete-source-intent:' + hashlib.sha256((account + ':' + str(fid)).encode()).hexdigest()
        intent = store.meta_json(intent_key, None)
        if intent and (not isinstance(intent, dict) or intent.get('source') != source
                       or intent.get('relative_path') != relative):
            return None, 'failed', '已有回收意图但身份不一致，未重复提交。'
        return {'account': account, 'fid': str(fid), 'pid': str(source['parent_id']),
                'relative': relative, 'source': source, 'path': path, 'root': root,
                'playback': pid, 'intent_key': intent_key, 'new_intent': intent is None}, None, ''
    except Exception as exc:
        detail = ('%s: %s' % (type(exc).__name__, exc)).strip()[:120]
        return None, 'failed', '核查失败（%s），已保留。' % detail


async def _verify_gone(engine, fid):
    """确认文件已离开网盘活动空间。一次 live 查询，不再全量翻回收站。"""
    try:
        return not await engine.cloud.live_file_exists(str(fid))
    except Exception:
        return False


def _finish_cloud_local(engine, info):
    store = engine.store
    try:
        from .fsops import purge_strm
        path = info['path']
        had_local = path.exists()
        result = purge_strm(path, info['root'], store=store) if had_local else {'file': 0, 'dirs': 0}
        if had_local and (result.get('file') != 1 or path.exists() or path.is_symlink()):
            raise OSError('本地 STRM 清理失败')
        removed_dirs = result.get('dirs', 0)
        with store.db:
            store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=? AND file_id=?', (info['playback'], info['fid']))
            store.db.execute('DELETE FROM organized_files WHERE file_id=? AND playback_id=? AND relative_path=?',
                             (info['fid'], info['playback'], info['relative']))
            store.db.execute('UPDATE output_registry SET active=0 WHERE account_id=? AND file_id=?',
                             (info['account'], info['fid']))
            store.db.execute('DELETE FROM owned_outputs WHERE relative_path=? AND account_id=? AND file_id=?',
                             (info['relative'], info['account'], info['fid']))
            store.db.execute('DELETE FROM meta WHERE key=?', (info['intent_key'],))
        return removed_dirs, had_local
    except Exception as exc:
        raise RuntimeError('网盘已回收，本地 STRM 或登记清理未完成；请核对后处理') from exc


async def guarded_managed_delete(engine, target):
    """核验 → 提交一次回收 → 确认离开活动空间 → 本地清理。"""
    import asyncio
    info, status, message = _prepare_cloud(engine, target)
    if info is None:
        return status, message, []
    store = engine.store
    try:
        if info['new_intent']:
            actual = await engine.cloud.file(info['fid'])
            source = info['source']
            if (any(actual.get(key) != source.get(key) for key in ('id', 'name', 'parent_id', 'size'))
                    or str(actual.get('sha1') or '').upper() != source['sha1'].upper()):
                return 'skipped', '网盘来源文件身份或位置发生变化，已保留。', []
            before = engine.cloud.recycle_snapshot()
            store.set_meta(info['intent_key'], {'source': source, 'relative_path': info['relative'],
                                                'at': time.time(),
                                                'before_rids': [entry['rid'] for entry in before][:200]
                                                if before else []})
            try:
                await engine.cloud.recycle(info['fid'], info['pid'])
            except asyncio.CancelledError:
                raise
            except Exception:
                # 响应可能丢失，读回才是权威依据。
                pass
        if not await _verify_gone(engine, info['fid']):
            return 'failed', '网盘回收结果尚未确认，保留本地文件且不会重复提交。', []
        removed_dirs, had_local = _finish_cloud_local(engine, info)
        notes = []
        if removed_dirs or not had_local:
            await prune_cloud_empty_chain(engine, info['pid'], notes)
        text = '已确认网盘文件离开活动空间；受管本地 STRM 与专属侧车已清理。'
        if notes:
            text += '；' + '；'.join(notes)
        return 'done', text, []
    except RuntimeError as exc:
        if str(exc).startswith('网盘已回收'):
            return 'failed', str(exc), []
        return 'failed', '核查或回收未完成（RuntimeError），已保留未确认的本地文件。', []
    except Exception as exc:
        return 'failed', '核查或回收未完成（%s），已保留未确认的本地文件。' % type(exc).__name__, []

async def guarded_managed_delete_many(engine, targets):
    """一批网盘删除：逐条核验 → 按父目录一次批量回收 → 逐条确认 → 本地清理。"""
    import asyncio
    results, infos = {}, []
    for target in targets:
        info, status, message = _prepare_cloud(engine, target)
        key = (target.get('relative_path'), str(target.get('file_id')))
        if info is None:
            results[key] = (False, message or '核查未通过')
        else:
            infos.append(info)
    fresh = []
    for info in infos:
        key = (info['relative'], info['fid'])
        if not info['new_intent']:
            continue
        try:
            actual = await engine.cloud.file(info['fid'])
        except Exception:
            results[key] = (False, '网盘来源读取失败，已保留。')
            continue
        source = info['source']
        if (any(actual.get(field) != source.get(field) for field in ('id', 'name', 'parent_id', 'size'))
                or str(actual.get('sha1') or '').upper() != source['sha1'].upper()):
            results[key] = (False, '网盘来源文件身份或位置发生变化，已保留。')
            continue
        fresh.append(info)
    groups = {}
    for info in fresh:
        groups.setdefault(info['pid'], []).append(info)
    for pid, group in groups.items():
        stamp = time.time()
        for info in group:
            engine.store.set_meta(info['intent_key'], {'source': info['source'],
                                                       'relative_path': info['relative'], 'at': stamp})
        try:
            await engine.cloud.recycle_many([info['fid'] for info in group], pid)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 响应可能丢失，下面的逐条确认才是权威依据。
            pass
    pruned = set()
    works = set()
    for info in infos:
        key = (info['relative'], info['fid'])
        if key in results:
            continue
        if not await _verify_gone(engine, info['fid']):
            results[key] = (False, '网盘回收结果尚未确认，保留本地文件且不会重复提交。')
            continue
        try:
            removed_dirs, had_local = _finish_cloud_local(engine, info)
        except Exception:
            results[key] = (False, '网盘已回收，本地 STRM 或登记清理未完成；请核对后处理。')
            continue
        if (removed_dirs or not had_local) and info['pid'] not in pruned:
            try:
                notes = []
                await prune_cloud_empty_chain(engine, info['pid'], notes)
                pruned.add(info['pid'])
            except Exception:
                pass
        results[key] = (True, '已确认网盘文件离开活动空间；本地 STRM 已清理。')
        works.add(info['relative'].rsplit('/', 1)[0] if '/' in info['relative'] else info['relative'])
    # 整部作品删空后，把空的 TMDB 目录一起收掉（单条路径里由 cleanup_empty_work 负责）。
    if works:
        from .inventory_delete_adapter import cleanup_empty_work
        for relative in works:
            try:
                await asyncio.to_thread(cleanup_empty_work, engine, relative)
            except Exception:
                pass
    return results


async def apply(engine, targets, dry_run):
    """执行或预演删除。网盘侧只进回收站；本地 strm 与 NFO 一并清理。"""
    store, root = engine.store, engine.root
    if targets.get('managed') and not dry_run:
        raise RuntimeError('网盘真删必须经由持久队列与回收确认。')
    result = {'dry_run': bool(dry_run), 'managed': [], 'share': [], 'errors': []}

    def _entry(entry, action):
        entry['actions'].append(action)
        return entry

    for row in targets['managed']:
        entry = {'file_id': row['file_id'], 'relative_path': row['relative_path'], 'actions': []}
        local = root / row['relative_path']
        # 托管真删只允许走 guarded_managed_delete（持久队列 + 回收确认）。
        # 这里只做预演，绝不留第二条真删路径。
        if local.is_file():
            entry = _entry(entry, '（预演）将删除本地 STRM')
        if local.with_suffix('.nfo').exists():
            entry = _entry(entry, '（预演）将删除本地 NFO')
        entry = _entry(entry, '（预演）将删除网盘文件（115 回收站可还原）')
        result['managed'].append(entry)

    for row in targets['share']:
        entry = {'strm_path': row['strm_path'], 'actions': []}
        local = root / row['strm_path']
        try:
            if local.is_file():
                if not dry_run:
                    from .fsops import purge_strm
                    purge_strm(local, root, store=store)
                entry = _entry(entry, '已删除本地 STRM')
            if dry_run:
                entry = _entry(entry, '（预演）分享来源不涉及网盘文件')
                result['share'].append(entry)
                continue
            with store.db:
                store.db.execute('UPDATE share_strms SET revoked=1 WHERE id=?', (row['id'],))
            entry = _entry(entry, '已确认删除（分享来源，不涉及网盘文件）')
            result['share'].append(entry)
        except Exception as exc:
            result['errors'].append({**entry, 'actions': ['失败：%s' % str(exc)[:80]]})
    return result


# ---- 队列：webhook 只入队，后台按序删 ----

def enqueue(store, targets, dry_run, source=''):
    """把命中项写进队列，返回 (批次号, 条数)。webhook 不再等删除完成。"""
    now = time.time()
    batch = '%.3f' % now
    rows = []
    for row in targets['managed']:
        rows.append((batch, 'managed', row['file_id'], row.get('playback_id') or '',
                     row['relative_path'], '', source, 1 if dry_run else 0))
    for row in targets['share']:
        rows.append((batch, 'share', '', '', row['strm_path'], row['id'], source,
                     1 if dry_run else 0))
    with store.db:
        for row in rows:
            store.db.execute(
                'INSERT INTO deep_delete_queue(batch,kind,file_id,playback_id,relative_path,'
                'share_id,source,dry_run,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                row + (now, now))
    return batch, len(rows)


def pending(store, limit=3):
    return store.db.execute(
        "SELECT * FROM deep_delete_queue WHERE state='queued' ORDER BY id LIMIT ?",
        (max(1, int(limit)),)).fetchall()


def mark(store, row, state, message):
    with store.db:
        store.db.execute('UPDATE deep_delete_queue SET state=?,message=?,updated=? WHERE id=?',
                         (state, message, time.time(), row['id']))


def progress(store, batch):
    """把这一批的进度写进 meta（面板展示），全部结束再记一条「完成」。"""
    rows = store.db.execute('SELECT state, count(*) AS n FROM deep_delete_queue WHERE batch=? '
                            'GROUP BY state', (batch,)).fetchall()
    counts = {row['state']: row['n'] for row in rows}
    done, skipped, failed = counts.get('done', 0), counts.get('skipped', 0), counts.get('failed', 0)
    total = done + skipped + failed + counts.get('queued', 0)
    first = store.db.execute('SELECT * FROM deep_delete_queue WHERE batch=? ORDER BY id LIMIT 1',
                             (batch,)).fetchone()
    mode = '预演' if first and first['dry_run'] else '执行'
    details = [{'path': row['relative_path'] or row['share_id'] or row['file_id'],
                'actions': [row['message']] if row['message'] else [],
                'ok': row['state'] == 'done'}
               for row in store.db.execute(
                   "SELECT * FROM deep_delete_queue WHERE batch=? AND state!='queued' ORDER BY id LIMIT 100",
                   (batch,))]
    store.set_meta('deep_delete_last', {'at': time.time(), 'mode': mode,
                                        'path': (first['source'] if first else ''),
                                        'total': total, 'done': done, 'skipped': skipped,
                                        'failed': failed, 'errors': failed, 'details': details})
    if counts.get('queued', 0) == 0 and total:
        store.event('deep_delete', '深度删除 · 完成：成功 %d 条、跳过 %d 条、失败 %d 条（%s）'
                    % (done, skipped, failed, mode))
    return counts


async def run_row(engine, row):
    """执行队列里的一条；台账已变的直接跳过（断点续跑安全）。"""
    store = engine.store
    dry_run = bool(row['dry_run'])
    current_settings = store.get('sync_config', {})
    if not isinstance(current_settings, dict):
        raise ValueError('深度删除设置无法核实，未执行。')
    # A queued execution must not outlive a switch back to preview mode.
    # Do not reinterpret it as a preview, which would falsify batch reporting.
    if not dry_run and current_settings.get('deep_delete_dry_run', True) is not False:
        return 'skipped', '当前已切回预演，旧执行队列未执行；请重新发起预演。', []
    if row['kind'] == 'managed':
        current = store.db.execute(
            'SELECT file_id, playback_id, relative_path FROM organized_files WHERE file_id=?',
            (row['file_id'],)).fetchone()
        if not current:
            return 'skipped', '台账已无该文件（可能已删除）', []
        if current['relative_path'] != row['relative_path'] or current['playback_id'] != row['playback_id']:
            return 'skipped', '文件路径或播放身份在排队后变化，已保留。', []
        targets = {'managed': [dict(current)], 'share': []}
    else:
        current = store.db.execute(
            'SELECT id, strm_path FROM share_strms WHERE id=? AND revoked=0',
            (row['share_id'],)).fetchone()
        if not current:
            return 'skipped', '分享条目已撤销或不存在', []
        if current['strm_path'] != row['relative_path']:
            return 'skipped', '分享路径在排队后变化，已保留。', []
        targets = {'managed': [], 'share': [dict(current)]}
    # Recheck markers immediately before execution; read errors fail closed.
    targets, dropped = drop_internal(store, targets)
    if dropped:
        return 'skipped', '本地路径已登记为程序内部删除，已保留源文件。', []
    if row['kind'] == 'managed' and not dry_run:
        return await guarded_managed_delete(engine, dict(current))
    result = await apply(engine, targets, dry_run)
    if result['errors']:
        return 'failed', '、'.join(result['errors'][0].get('actions') or ['失败']), []
    entry = (result['managed'] or result['share'] or [{}])[0]
    actions = entry.get('actions') or []
    if not actions:
        return 'skipped', '没有可删除的内容（本地与网盘都不在了）', []
    return 'done', '；'.join(actions), actions


_SEASON_DIR = re.compile(r'/Season \d+$', re.IGNORECASE)


def _group_items(items):
    """把一批明细按「作品 · 季」聚合（电影/分享单片一组），面板折叠展示用。"""
    order = {}
    for item in items:
        rel = str(item.get('relative_path') or '').strip('/')
        share = str(item.get('share_id') or '')
        fid = str(item.get('file_id') or '')
        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        if _SEASON_DIR.search(parent):
            work = parent.rsplit('/', 1)[0]
            key, kind = parent, 'season'
            title = work.rsplit('/', 1)[-1] if '/' in work else work
            season = parent.rsplit('/', 1)[-1]
        elif share:
            key, kind, work = 'share:' + share, 'share', parent
            title = rel.rsplit('/', 1)[-1] if '/' in rel else share
            season = ''
        else:
            key = parent or rel or ('file:' + fid)
            kind = 'single'
            work = parent
            title = parent.rsplit('/', 1)[-1] if '/' in parent else (rel.rsplit('/', 1)[-1] if '/' in rel else (fid or '未知'))
            season = ''
        if key not in order:
            order[key] = {'key': key, 'kind': kind, 'work': work, 'title': title,
                          'season': season, 'total': 0, 'done': 0, 'skipped': 0,
                          'failed': 0, 'items': [], 'items_total': 0}
        group = order[key]
        group['total'] += 1
        state = item.get('state') if item.get('state') in ('done', 'skipped', 'failed') else 'skipped'
        group[state] += 1
        if len(group['items']) < 200:
            group['items'].append({'path': rel or share or fid, 'state': item.get('state') or 'skipped',
                                   'message': item.get('message') or ''})
    groups = list(order.values())
    for group in groups:
        group['items_total'] = group['total']
    groups.sort(key=lambda g: (g['kind'] != 'single', g['kind'] != 'share', g['work'] or g['title']))
    return groups


async def notify_batch(engine, batch):
    """一批深度删除全部结束后发一条 TG 汇总（预演不发，避免刷屏）。"""
    notifications = getattr(engine, 'notifications', None)
    if notifications is None:
        return
    store = engine.store
    first = store.db.execute('SELECT * FROM deep_delete_queue WHERE batch=? ORDER BY id LIMIT 1',
                             (batch,)).fetchone()
    if not first or first['dry_run']:
        return
    rows = store.db.execute('SELECT * FROM deep_delete_queue WHERE batch=? ORDER BY id', (batch,)).fetchall()
    if not rows:
        return
    done = sum(1 for r in rows if r['state'] == 'done')
    skipped = sum(1 for r in rows if r['state'] == 'skipped')
    failed = sum(1 for r in rows if r['state'] == 'failed')
    lines = ['成功 %d · 跳过 %d · 失败 %d' % (done, skipped, failed)]
    sample = [r['relative_path'] or r['share_id'] or r['file_id'] for r in rows[:3]]
    for path in sample:
        lines.append('· ' + (path or '')[:80])
    if len(rows) > 3:
        lines.append('… 共 %d 条' % len(rows))
    await notifications.publish('deep_delete:%s' % batch, '🧹 深度删除完成',
                                '\n'.join(lines), 'success' if not failed else 'warning',
                                category='general')


def history(store, limit=20):
    """最近若干批深度删除（含逐条明细），面板「历史」用。"""
    limit = max(1, min(100, int(limit)))
    batches = store.db.execute(
        "SELECT batch, min(created) AS at, max(dry_run) AS dry_run, max(source) AS source,"
        " count(*) AS total,"
        " sum(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done,"
        " sum(CASE WHEN state='skipped' THEN 1 ELSE 0 END) AS skipped,"
        " sum(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,"
        " sum(CASE WHEN state='queued' THEN 1 ELSE 0 END) AS queued"
        " FROM deep_delete_queue GROUP BY batch ORDER BY batch DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in batches:
        items = store.db.execute(
            'SELECT * FROM deep_delete_queue WHERE batch=? ORDER BY id', (row['batch'],)).fetchall()
        details = [{'path': item['relative_path'] or item['share_id'] or item['file_id'],
                    'actions': [item['message']] if item['message'] else [],
                    'state': item['state'], 'ok': item['state'] == 'done'}
                   for item in items[:100]]
        groups = _group_items([dict(item) for item in items])
        rollup = {'works': sum(1 for g in groups if g['kind'] != 'share'),
                  'seasons': sum(1 for g in groups if g['kind'] == 'season'),
                  'files': len(items), 'groups': len(groups)}
        result.append({'batch': row['batch'], 'at': row['at'],
                       'mode': '预演' if row['dry_run'] else '执行',
                       'source': row['source'] or '', 'total': row['total'],
                       'done': row['done'] or 0, 'skipped': row['skipped'] or 0,
                       'failed': row['failed'] or 0, 'queued': row['queued'] or 0,
                       'details': details, 'groups': groups, 'rollup': rollup})
    return {'batches': result}


async def drain(engine, limit=3):
    """后台 worker 调它：按序处理队列，返回本轮条数。

    整理/转存正在跑时先让路（这些文件可能正被使用）；停机时立刻收手。
    """
    store = engine.store
    if getattr(engine, 'closing', False):
        return 0
    if store.db.execute("SELECT 1 FROM organize_jobs WHERE state='running' LIMIT 1").fetchone():
        return 0
    rows = pending(store, limit)
    if not rows:
        return 0
    batches = set()
    for row in rows:
        if getattr(engine, 'closing', False):
            break
        try:
            # Serialize marker checks and destructive work with local writers.
            gate = getattr(engine, 'publication_gate', None)
            if gate is None:
                state, message, _ = await run_row(engine, row)
            else:
                async with gate:
                    state, message, _ = await run_row(engine, row)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮队列
            state, message = 'failed', '执行失败：%s' % str(exc)[:120]
        mark(store, row, state, message)
        label = row['relative_path'] or row['share_id'] or row['file_id']
        store.event('deep_delete', '深度删除 · %s：%s → %s'
                    % ({'done': '执行', 'skipped': '跳过', 'failed': '失败'}[state], label, message))
        batches.add(row['batch'])
        counts = progress(store, row['batch'])
        if counts.get('queued', 0) == 0:
            await notify_batch(engine, row['batch'])
    return len(rows)
