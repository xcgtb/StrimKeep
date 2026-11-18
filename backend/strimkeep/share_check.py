"""分享失效检测：确认分享已取消且本地 STRM 归属可核实时才清理。

独立于 share_service 的任务/生成流程，避免检测逻辑继续和整理状态机混在一起。
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path

from .fsops import purge_strm
from .share_urls import share_play_path_matches


def _verified_share_strm(root, relative, share_id):
    """Return an owned regular STRM only if its path and embedded ID agree."""
    from urllib.parse import urlsplit
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(part in ('.', '..') for part in rel.parts):
        return None
    path = root / rel
    try:
        cursor = root
        for part in rel.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size > 4096:
            return None
        content = path.read_text(encoding='utf-8').strip()
        if not share_play_path_matches(urlsplit(content).path, str(share_id)):
            return None
        return path
    except (OSError, UnicodeError, ValueError):
        return None


async def check_alive(engine):
    """按分享核验；仅在确认取消且归属可核实时撤销。"""
    store, root = engine.store, engine.root
    rows = store.db.execute(
        'SELECT id, name, share_code, receive_code, file_id, strm_path FROM share_strms WHERE revoked=0').fetchall()
    scope = 'share'
    dead, alive, skipped, details = [], 0, 0, []
    share_checks = 0
    status_cache = {}
    settings = store.get('share_settings', {}) or {}
    try:
        concurrency = max(1, min(4, int(settings.get('share_check_concurrency', 3))))
    except (TypeError, ValueError):
        concurrency = 3
    groups = {}
    for row in rows:
        groups.setdefault((row['share_code'], row['receive_code']), []).append(row)
    keys = list(groups)
    started = time.time()
    engine._share_check_progress = {'started_at': started, 'total': len(rows), 'processed': 0, 'alive': 0, 'dead': 0, 'skipped': 0, 'scope': scope,
                                    'total_shares': len(keys), 'checked_shares': 0, 'concurrency': concurrency}
    await asyncio.to_thread(store.set_meta, 'share_check_progress', dict(engine._share_check_progress))

    def detail(row, status, reason):
        if len(details) < 200:
            details.append({'id': row['id'], 'name': row['name'] or '', 'path': row['strm_path'] or '', 'status': status, 'reason': reason})

    async def process_group(share_key, group_rows):
        nonlocal alive, skipped
        for row in group_rows:
            engine._share_check_progress.update(processed=alive + len(dead) + skipped, alive=alive, dead=len(dead), skipped=skipped)
            await asyncio.to_thread(store.set_meta, 'share_check_progress',
                                    dict(engine._share_check_progress))
            try:
                share_state, observed_at = status_cache[share_key]
                if share_state == 'accessible':
                    alive += 1
                    with store.db:
                        store.db.execute('DELETE FROM share_check_candidates WHERE id=?', (row['id'],))
                    continue
                if share_state != 'cancelled':
                    skipped += 1
                    detail(row, 'unknown', '分享状态未能确认，已保留')
                    continue
                # 两次独立检测至少间隔 5 分钟；记录只保存身份摘要，不保存分享码。
                identity = hashlib.sha256(json.dumps([row['share_code'], row['receive_code'], row['file_id']],
                                                      ensure_ascii=False).encode()).hexdigest()
                candidate = store.db.execute('SELECT identity, first_at FROM share_check_candidates WHERE id=?', (row['id'],)).fetchone()
                if not candidate or candidate['identity'] != identity or observed_at - candidate['first_at'] < 0:
                    with store.db:
                        store.db.execute('INSERT OR REPLACE INTO share_check_candidates VALUES (?,?,?,?)',
                                         (row['id'], identity, observed_at, observed_at))
                    skipped += 1
                    detail(row, 'pending', '115 返回分享取消，至少 5 分钟后再次检测确认；本地文件已保留')
                    continue
                if observed_at - candidate['first_at'] < 300:
                    skipped += 1
                    detail(row, 'pending', '距离首次核验不足 5 分钟，暂不清理')
                    continue
                # Check the live row under the publication lock after the network
                # wait: a concurrent organiser may have changed or moved it.
                async with engine.publication_gate:
                    current = store.db.execute(
                        'SELECT id,share_code,receive_code,file_id,strm_path,revoked FROM share_strms WHERE id=?',
                        (row['id'],)).fetchone()
                    fields = ('id', 'share_code', 'receive_code', 'file_id', 'strm_path')
                    if not current or current['revoked'] or any(current[k] != row[k] for k in fields):
                        skipped += 1
                        detail(row, 'unknown', '检查期间条目发生变化，已保留')
                        continue
                    path = _verified_share_strm(root, current['strm_path'], current['id'])
                    if path is None:
                        skipped += 1
                        detail(row, 'unknown', '本地 STRM 归属无法确认，已保留')
                        store.event('share_check', '失效分享的本地文件归属无法确认，已保留并跳过。')
                        continue
                    # Remove the STRM and its own sidecars; the directory-level
                    # sidecars are cleared only when no other share STRM remains.
                    try:
                        purge_strm(path, root)
                    except (OSError, RuntimeError):
                        skipped += 1
                        detail(row, 'unknown', '清理本地文件失败，请检查日志及权限')
                        continue
                    with store.db:
                        changed = store.db.execute(
                            'DELETE FROM share_strms WHERE id=? AND revoked=0 AND strm_path=?',
                            (row['id'], row['strm_path'])).rowcount
                        if changed == 1:
                            # 死分享的 STRM 和登记一起清掉，不留「已撤销」历史。
                            store.db.execute(
                                "DELETE FROM native_media_info_queue WHERE json_extract(target,'$.relative')=?",
                                (current['strm_path'],))
                    if changed != 1:
                        skipped += 1
                        detail(row, 'unknown', '条目状态已变化，请重新检查')
                        continue
                    with store.db:
                        store.db.execute('DELETE FROM share_check_candidates WHERE id=?', (row['id'],))
                    dead.append({'id': row['id'], 'name': row['name'] or '', 'strm_path': row['strm_path']})
                    detail(row, 'dead', '两次检测均确认分享已取消，本地文件已清理')
            except Exception:
                skipped += 1
                detail(row, 'unknown', '请求异常，无法确认分享状态')
                continue
    async def check_shares(iterator):
        for key in iterator:
            try:
                state = await engine.cloud.share_availability(*key)
            except Exception:
                state = 'unknown'
            status_cache[key] = (state, time.time())
            engine._share_check_progress['checked_shares'] += 1
            await asyncio.to_thread(store.set_meta, 'share_check_progress',
                                    dict(engine._share_check_progress))
            await process_group(key, groups[key])

    iterator = iter(keys)
    await asyncio.gather(*(check_shares(iterator) for _ in range(min(concurrency, len(keys)))))
    share_checks = len(status_cache)
    finished = time.time()
    report = {'at': finished, 'started_at': started, 'total': len(rows), 'alive': alive,
              'dead': len(dead), 'skipped': skipped, 'scope': scope, 'share_checks': share_checks, 'concurrency': concurrency, 'details': details,
              'details_truncated': max(0, len(dead) + skipped - len(details))}
    history = store.meta_json('share_check_history', []) or []
    previous = store.meta_json('share_check_last', {}) or {}
    if not history and previous.get('at'):
        history = [{**previous, 'total': sum(previous.get(k, 0) for k in ('alive', 'dead', 'skipped')), 'legacy': True, 'details': []}]
    store.set_meta('share_check_history', ([report] + history)[:20])
    store.set_meta('share_check_last', {'at': finished, 'alive': alive, 'dead': len(dead), 'skipped': skipped})
    engine._share_check_progress.update(processed=len(rows), alive=alive, dead=len(dead), skipped=skipped)
    store.event('share_check', '分享检测完成：可访问 %d 条，失效已清理 %d 条，无法判定 %d 条。' % (alive, len(dead), skipped))
    return {'alive': alive, 'dead': dead, 'dead_count': len(dead), 'skipped': skipped, 'scope': scope, 'share_checks': share_checks}


