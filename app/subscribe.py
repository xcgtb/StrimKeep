# -*- coding: utf-8 -*-
"""从 engine.py 拆出。跨层符号统一经 _eng() 惰性访问（monkeypatch 穿透 + 双导入兼容）。"""
if __package__:
    from . import state_store as _state
else:
    import state_store as _state
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback, copy, uuid
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

log = logging.getLogger('strimkeep')

_notification_lock = threading.RLock()
_notification_drain_lock = threading.RLock()
_subscription_check_lock = threading.RLock()
_subscription_observation_lock = threading.Lock()
_NOTIFICATION_SCHEMA = 1
_NOTIFICATION_HISTORY_LIMIT = 256
_NOTIFICATION_PENDING_LIMIT = 512
_NOTIFICATION_BYTES_LIMIT = 8 * 1024 * 1024


class NotificationBackpressure(RuntimeError):
    """Never discard accepted events or acknowledge new changes when full."""

try:
    from .core import (esc, parse_season_dir, get_ep, title_key,
                       governance_title_key, analyze_season_episodes, parse_emby_library,
                       quality_label, RE_SXXEXX)
except ImportError:
    from core import (esc, parse_season_dir, get_ep, title_key,
                      governance_title_key, analyze_season_episodes, parse_emby_library,
                      quality_label, RE_SXXEXX)


try:
    from . import config as _cfg
    from . import logger
    from . import storage_status as _storage_status
except ImportError:
    import config as _cfg
    import logger
    import storage_status as _storage_status


def _eng():
    try:
        from . import engine as _e
        return _e
    except ImportError:
        try:
            import engine as _e
            return _e
        except ImportError:
            return None


def _load_sub_state() -> dict:
    try:
        data = _state.read(_eng().SUB_STATE_FILE, {})
        if not isinstance(data, dict):
            raise ValueError('invalid subscription state')
        return data
    except (OSError, ValueError) as error:
        raise RuntimeError('订阅状态无法读取，本轮检查已停止，原状态保留') from error

def _save_sub_state(state: dict):
    _state.save(_eng().SUB_STATE_FILE, state)


def _notification_file():
    state_path = getattr(_eng(), 'SUB_STATE_FILE', None)
    if state_path is not None and getattr(state_path, 'name', '') != 'subscriptions_state.json':
        # Isolated tests and legacy migrations may swap the subscription state
        # document; keep their notification journal isolated as well.
        state_path = Path(state_path)
        return state_path.with_name('subscription_notifications_' + state_path.stem + '.json')
    return getattr(_eng(), 'SUB_NOTIFICATION_FILE', _eng().STATE_DIR / 'subscription_notifications.json')


def _load_notification_queue() -> dict:
    data = _state.read(_notification_file(), {'schema': _NOTIFICATION_SCHEMA, 'events': []})
    if (not isinstance(data, dict) or not isinstance(data.get('events', []), list) or
            any(not isinstance(e, dict) or e.get('status') not in ('pending', 'sending', 'delivered', 'sent')
                for e in data.get('events', []))):
        raise RuntimeError('追更待通知队列损坏，本轮发送已停止，队列原样保留')
    data.setdefault('schema', _NOTIFICATION_SCHEMA)
    return data


def _bounded_notification_history(data: dict):
    data = dict(data or {})
    data['schema'] = _NOTIFICATION_SCHEMA
    events = list(data.get('events') or [])
    # Keep a bounded sent history for crash recovery/deduplication while
    # allowing pending events to survive indefinitely until delivery succeeds.
    sent = [e for e in events if e.get('status') == 'sent'][-_NOTIFICATION_HISTORY_LIMIT:]
    sent = [e if e.get('state_error') else {
        k: e[k] for k in ('id', 'status', 'sent_at', 'canceled_at', 'reason', 'attempts') if k in e
    } for e in sent]
    pending = [e for e in events if e.get('status') != 'sent']
    data['events'] = pending + sent
    return data


def _save_notification_queue(data: dict):
    data = _bounded_notification_history(data)
    pending = [e for e in data['events'] if e.get('status') != 'sent']
    _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state.save(_notification_file(), data)
    # A small document lets Web polling show backlog without loading every
    # event. Failure of this derived view must not roll back accepted events.
    try:
        _state.save(_notification_file().with_name(_notification_file().stem + '_status.json'), {
            'pending': len(pending), 'limit': _NOTIFICATION_PENDING_LIMIT,
            'last_error': next((e.get('error') for e in reversed(pending) if e.get('error')), ''),
            'updated_at': time.time(),
        })
    except Exception as error:
        log.warning('追更队列摘要更新失败（通知队列已保留）: %s', error)


def load_subscription_notification_status():
    status = dict(_state.read(_notification_file().with_name(_notification_file().stem + '_status.json'), {}))
    repair = _state.read(_notification_reconcile_file(), {})
    status['reconcile_required'] = bool(repair.get('required'))
    return status


def _notification_reconcile_file():
    return _notification_file().with_name(_notification_file().stem + '_reconcile.json')


def _mark_notification_reconcile():
    with _notification_lock:
        _state.save(_notification_reconcile_file(), {
            'required': True, 'generation': uuid.uuid4().hex, 'last_attempt': 0,
        })


def recover_subscription_notifications():
    """After backpressure/failed enqueue, recheck subscribed facts, not media.

    Called only by the single notification worker, no more than every five
    minutes. Compare generations so a concurrent new failure cannot be cleared
    by an older repair. Already queued batches are not recreated or merged.
    """
    with _notification_lock:
        repair = _state.read(_notification_reconcile_file(), {})
        if not repair.get('required') or time.time() - float(repair.get('last_attempt') or 0) < 300:
            return {'status': 'skipped'}
        queue = _load_notification_queue()
        if sum(e.get('status') != 'sent' for e in queue.get('events') or []) >= max(1, _NOTIFICATION_PENDING_LIMIT // 2):
            return {'status': 'waiting'}
        generation = repair.get('generation')
        repair['last_attempt'] = time.time()
        _state.save(_notification_reconcile_file(), repair)
    result = process_subscription_tmdb_calibration(dispatch=False)
    if result.get('status') == 'success':
        with _notification_lock:
            current = _state.read(_notification_reconcile_file(), {})
            if current.get('generation') == generation:
                _state.save(_notification_reconcile_file(), {'required': False})
    return result


def _render_notification(updates):
    lines = [_eng().tg_title('🔔', '追更订阅', f'{len(updates)} 部有变化')]
    for update in updates:
        lines.append('')
        lines.append(f"📺 <b>《{html.escape(str(update.get('name') or ''))}》</b>")
        if update.get('refilled'):
            lines.append(f"　✅ 已补齐 <b>{_fmt_ep_ranges(_keys_to_eps(update['refilled']))}</b>")
        if update.get('new_eps'):
            lines.append(f"　🆕 新增入库 <b>{_fmt_ep_ranges(_keys_to_eps(update['new_eps']))}</b>")
        if update.get('newly_missing'):
            lines.append(f"　⚠️ 仍然缺集 <b>{_fmt_ep_ranges(_keys_to_eps(update['newly_missing']))}</b>")
        # A newly arrived episode may leave a known gap.  It is an attachment
        # to this update, never a separate repeating alarm.
        if update.get('new_eps') and update.get('missing_eps'):
            remaining = (_keys_to_eps(update['missing_eps']) -
                         _keys_to_eps(update.get('newly_missing') or []))
            if remaining:
                lines.append(f"　⚠️ 仍然缺集 <b>{_fmt_ep_ranges(remaining)}</b>")
    return '\n'.join(lines)


def _render_notification_card(update):
    """One show's caption. Bound display fields without changing queued facts."""
    def esc(value, limit=120):
        value = str(value or '')
        return html.escape(value if len(value) <= limit else value[:limit] + '…')

    def episodes(keys):
        eps = _keys_to_eps(keys or [])
        text = _fmt_ep_ranges(eps)
        if len(text) > 100:
            # Cut at a complete range, never in the middle of an episode key.
            text = _fmt_ep_ranges(sorted(eps)[:6]) + f'…（共 {len(eps)} 集）'
        return esc(text, 120)

    lines = ['🔔 <b>追更订阅</b>',
             '📺 <b>《' + esc(update.get('name'), 100) + '》</b>'
             + (f"（{esc(update['year'], 4)}）" if update.get('year') else ''), '']
    if update.get('new_eps'):
        lines.append(f"🆕 新增入库 <b>{episodes(update['new_eps'])}</b> · {len(update['new_eps'])} 集")
    if update.get('refilled'):
        lines.append(f"✅ 已补齐 <b>{episodes(update['refilled'])}</b>")
    if update.get('newly_missing'):
        lines.append(f"⚠️ 新发现缺集 <b>{episodes(update['newly_missing'])}</b>")
    if update.get('missing_eps'):
        lines.append(f"🧩 待补 <b>{len(update['missing_eps'])} 集</b> · {episodes(update['missing_eps'])}")
    if update.get('have') is not None:
        total = f"📚 片库已有 <b>{update['have']} 集</b>"
        if update.get('tmdb_source') in ('tmdb', 'tmdb_cache') and update.get('tmdb_total') is not None:
            total += f" · TMDB 已播 {update['tmdb_total']} 集"
        lines.append(total)
    seasons = update.get('season_counts') or []
    if seasons:
        lines.append('🗓 ' + ' · '.join(f"S{int(s['season']):02d} 已有 {s['have']} 集" for s in seasons[:3])
                     + (f' · 另 {len(seasons) - 3} 季' if len(seasons) > 3 else ''))
    counts = []
    for field, label in (('local_eps', '本地库'), ('share_eps', '分享库')):
        if update.get(field) is not None:
            counts.append(f"{label} {update[field]} 集")
    if counts:
        lines.append('🗂 ' + ' · '.join(counts))
    if update.get('tmdb_id'):
        lines.append('🎞 TMDB：' + esc(update['tmdb_id'], 16))
    source = {'library_facts': 'Emby 片库事实', 'emby_live': 'Emby 实时核实',
              'disk': 'STRM 目录核实', 'disk_scan': 'STRM 目录核实'}.get(update.get('facts_source'))
    if source:
        lines.append('📌 ' + source)
    if update.get('tmdb_source') in ('unavailable', 'disabled'):
        lines.append('ℹ️ 本次未取得已播对照，缺集信息沿用上次记录')
    return '\n'.join(lines)


def _deliver_notification_cards(payload, deadline=None, should_stop=None):
    """Durable per-show receipts; partial retry never repeats accepted cards."""
    delivered = set(payload.get('delivered_sids') or [])
    for update in payload.get('updates') or []:
        sid = str(update.get('sid') or '')
        if sid in delivered:
            continue
        if ((should_stop is not None and should_stop()) or
                (deadline is not None and time.monotonic() >= deadline)):
            return None
        # Check pause/delete again between cards, outside the journal lock.
        enabled = {str(s.get('id') or s.get('tmdb_id') or s.get('name'))
                   for s in _cfg.get_subscriptions() if s.get('enabled', True)}
        if sid not in enabled or _cfg.load_config().get('subscribe_enabled', '1') != '1':
            return None
        if not _eng().notify_telegram_card(_render_notification_card(update), update.get('poster') or '',
                                         text_sender=_eng().notify_telegram):
            return False
        with _notification_lock:
            queue = _load_notification_queue()
            current = next((e for e in queue.get('events') or [] if e.get('id') == payload.get('id')), None)
            if current is None:
                raise RuntimeError('追更卡片已发送，但通知事件丢失')
            delivered.add(sid)
            current['delivered_sids'] = sorted(delivered)
            _save_notification_queue(queue)
    return True


def _queued_episode_claims(queue):
    """Unacknowledged changes already promised to a Telegram batch.

    Pending, sending and delivered (waiting for local state recovery) all own
    their episode keys.  A later ingest cycle must never re-claim them.
    """
    by_sid = defaultdict(lambda: defaultdict(set))
    for event in (queue or {}).get('events') or []:
        if event.get('status') not in ('pending', 'sending', 'delivered'):
            continue
        for update in event.get('updates') or []:
            sid = str(update.get('sid') or '')
            if not sid:
                continue
            try:
                stamp = float((event.get('state_after') or {}).get(sid, {}).get('facts_ts') or 0)
                by_sid[sid]['facts_ts'] = max(float(by_sid[sid].get('facts_ts') or 0), stamp)
            except (TypeError, ValueError):
                pass
            for field in ('new_eps', 'newly_missing', 'refilled'):
                by_sid[sid][field].update(update.get(field) or [])
    return by_sid


def _pending_claims_snapshot():
    with _notification_lock:
        return _queued_episode_claims(_load_notification_queue())


def _merge_subscription_record(before, after):
    """Never roll back an accepted episode when recovering an older event.

    State snapshots can be produced while an earlier Telegram message is in
    flight, so replacing the whole record would discard later acknowledgments.
    The strongest invariant is a monotonic set of successfully notified keys.
    """
    before = dict(before or {})
    after = dict(after or {})
    try:
        newer_before = float(before.get('facts_ts') or 0) > float(after.get('facts_ts') or 0)
    except (ValueError, TypeError):
        newer_before = False
    merged = {**after, **before} if newer_before else {**before, **after}
    seen = _keys_to_eps(before.get('notified_episodes')) | _keys_to_eps(after.get('notified_episodes'))
    if 'notified_episodes' in before or 'notified_episodes' in after:
        merged['notified_episodes'] = sorted(_eps_to_keys(seen))
    previous_ep = _parse_ep_key(before.get('latest_ep'))
    next_ep = _parse_ep_key(after.get('latest_ep'))
    last = max([x for x in (previous_ep, next_ep, *seen) if x], default=None)
    if last:
        merged['latest_ep'] = _ep_key(*last)
    return merged


def _persist_subscription_snapshots(staged):
    if not staged:
        return
    with _notification_lock:
        state = _load_sub_state()
        for sid, after in staged.items():
            state[str(sid)] = _merge_subscription_record(state.get(str(sid)), after)
        _save_sub_state(state)


def _queue_update_batch(updates, staged, reason='event'):
    """Create one immutable batch per ingest cycle, after filtering queued keys.

    Do not combine events across cycles, especially not a `delivered` event:
    its Telegram message was *already sent* and merging into it loses updates.
    """
    if not updates:
        return None
    with _notification_lock:
        queue = _bounded_notification_history(_load_notification_queue())
        claims = _queued_episode_claims(queue)
        acknowledged = _load_sub_state()
        fresh_updates = []
        for original in updates:
            update = copy.deepcopy(original)
            sid = str(update.get('sid') or '')
            if not sid:
                continue
            seen = _eps_to_keys(_keys_to_eps((acknowledged.get(sid) or {}).get('notified_episodes')))
            for field in ('new_eps', 'newly_missing', 'refilled'):
                excluded = claims[sid][field]
                if field in ('new_eps', 'refilled'):
                    excluded = excluded | seen
                update[field] = sorted(set(update.get(field) or []) - excluded)
            if any(update.get(field) for field in ('new_eps', 'newly_missing', 'refilled')):
                fresh_updates.append(update)
        if not fresh_updates:
            return None
        # A unique journal identity is needed even if a new broadcast later
        # happens to contain the same keys as a (now historical) batch.
        event = {
            'id': uuid.uuid4().hex,
            'created_at': time.time(), 'updated_at': time.time(),
            'status': 'pending', 'attempts': 0, 'reason': reason,
            'updates': fresh_updates,
            'state_after': copy.deepcopy({str(k): v for k, v in staged.items()
                                          if str(k) in {str(u['sid']) for u in fresh_updates}}),
        }
        queue.setdefault('events', []).append(event)
        pending_count = sum(e.get('status') != 'sent' for e in queue['events'])
        if (pending_count > _NOTIFICATION_PENDING_LIMIT or
                len(json.dumps(queue, ensure_ascii=False).encode('utf-8')) > _NOTIFICATION_BYTES_LIMIT):
            # Accepted batches are immutable. Do not merge across ingest cycles,
            # truncate old pending events, or advance new notification state.
            _mark_notification_reconcile()
            raise NotificationBackpressure('追更待通知队列已满，既有通知与未确认状态保留；恢复发送后校准补报')
        _save_notification_queue(queue)
        log.info('追更通知入队：%d 部（原因=%s，事件=%s）', len(fresh_updates), reason, event['id'])
        return event


def _apply_notification_state(event):
    _persist_subscription_snapshots(event.get('state_after') or {})

def drain_subscription_notifications(limit=8, force=False, time_budget=None, should_stop=None):
    """Retry persisted batches.  Returns delivery counts for diagnostics."""
    try:
        if _cfg.load_config().get('subscribe_enabled', '1') != '1':
            return {'status': 'disabled', 'sent': 0, 'failed': 0, 'pending': 0}
    except Exception as error:
        return {'status': 'error', 'sent': 0, 'failed': 1, 'pending': 0, 'error': str(error)}
    if not _notification_drain_lock.acquire(blocking=False):
        return {'status': 'busy', 'sent': 0, 'failed': 0, 'pending': 0}
    sent = failed = 0
    delivered_updates = []
    deadline = time.monotonic() + float(time_budget) if time_budget is not None else None
    try:
        for _ in range(max(1, int(limit or 1))):
            # Budget between batches; an individual existing Telegram call has
            # its own timeout. Never launch parallel retry/send workers.
            if ((should_stop is not None and should_stop()) or
                    (deadline is not None and time.monotonic() >= deadline)):
                break
            with _notification_lock:
                queue = _load_notification_queue()
                recovery = next((e for e in queue.get('events') or []
                                 if e.get('status') == 'delivered' or
                                 (e.get('status') == 'sent' and e.get('state_error'))), None)
                if recovery is not None:
                    try:
                        _apply_notification_state(recovery)
                        recovery['status'] = 'sent'
                        recovery.pop('state_error', None)
                        _save_notification_queue(queue)
                        continue
                    except Exception as exc:
                        recovery['state_error'] = str(exc)
                        _save_notification_queue(queue)
                        return {'status': 'partial', 'sent': sent, 'failed': failed + 1,
                                'pending': sum(e.get('status') != 'sent' for e in queue.get('events') or [])}
                now = time.time()
                event = next((e for e in queue.get('events') or []
                              if e.get('status') == 'sending' or
                              (e.get('status') == 'pending' and
                               (force or float(e.get('next_retry_at') or 0) <= now))), None)
                if event is None:
                    pending = sum(e.get('status') != 'sent' for e in queue.get('events') or [])
                    return {'status': 'success', 'sent': sent, 'failed': failed,
                            'pending': pending, 'delivered_updates': delivered_updates}
                # A user can pause a subscription while Telegram is offline.
                # Drop that show's pending item before sending; pausing is a
                # hard notification boundary just like the event matcher.
                try:
                    enabled_ids = {str(s.get('id') or s.get('tmdb_id') or s.get('name'))
                                   for s in _cfg.get_subscriptions() if s.get('enabled', True)}
                    kept_updates = [u for u in event.get('updates') or []
                                     if (str(u.get('sid') or '') in enabled_ids or
                                         str(u.get('sid') or '') in (event.get('delivered_sids') or []))]
                    if len(kept_updates) != len(event.get('updates') or []):
                        event['updates'] = kept_updates
                        event['state_after'] = {sid: value for sid, value in
                                                (event.get('state_after') or {}).items()
                                                if sid in {str(u.get('sid') or '') for u in kept_updates}}
                        if not kept_updates:
                            event['status'] = 'sent'
                            event['canceled_at'] = time.time()
                            _save_notification_queue(queue)
                            continue
                        _save_notification_queue(queue)
                except Exception as error:
                    # Fail closed: config unreadable is not permission to send
                    # a potentially paused/deleted subscription's old message.
                    return {'status': 'error', 'sent': sent, 'failed': failed + 1,
                            'pending': sum(e.get('status') != 'sent' for e in queue.get('events') or []),
                            'error': str(error)}
                # A crashed process may leave ``sending`` behind.  It is safe
                # to retry: Telegram lacks a local exactly-once protocol.
                event['status'] = 'sending'
                event['attempts'] = int(event.get('attempts') or 0) + 1
                event['last_attempt_at'] = time.time()
                _save_notification_queue(queue)
                payload = copy.deepcopy(event)
            ok = False
            error = ''
            try:
                ok = _deliver_notification_cards(payload, deadline, should_stop)
                if ok is False:
                    error = 'Telegram 推送失败'
            except Exception as exc:
                error = str(exc)
            with _notification_lock:
                queue = _load_notification_queue()
                current = next((e for e in queue.get('events') or [] if e.get('id') == payload.get('id')), None)
                if current is None:
                    continue
                if ok is None:
                    current['status'] = 'pending'
                    current['next_retry_at'] = 0
                    _save_notification_queue(queue)
                    break
                if ok:
                    # ``delivered`` is a recoverable intermediate marker: if
                    # the process dies after Telegram accepts the message but
                    # before the local subscription state is committed, the
                    # next drain applies state_after without sending again.
                    current['status'] = 'delivered'
                    current['sent_at'] = time.time()
                    current['error'] = ''
                    _save_notification_queue(queue)
                    try:
                        _apply_notification_state(current)
                    except Exception as exc:
                        # Keep the sent marker and state_after for recovery; a
                        # later drain applies it without sending again.
                        current['state_error'] = str(exc)
                        _save_notification_queue(queue)
                        failed += 1
                        log.warning('追更通知已发送但状态写回失败，将恢复写回：%s', exc)
                        continue
                    current['status'] = 'sent'
                    _save_notification_queue(queue)
                    sent += 1
                    delivered_updates.extend(copy.deepcopy(current.get('updates') or []))
                    _save_subscription_report(current.get('updates') or [])
                    log.info('追更通知发送成功：事件=%s', current.get('id'))
                else:
                    current['status'] = 'pending'
                    current['error'] = error or 'Telegram 推送失败'
                    current['next_retry_at'] = time.time() + min(1800, 30 * (2 ** min(6, int(current.get('attempts') or 1) - 1)))
                    _save_notification_queue(queue)
                    failed += 1
                    log.warning('追更通知发送失败，保留待重试事件=%s：%s', current.get('id'), current['error'])
                    break
        with _notification_lock:
            queue = _load_notification_queue()
            pending = sum(e.get('status') != 'sent' for e in queue.get('events') or [])
        return {'status': 'partial' if failed else 'success', 'sent': sent, 'failed': failed,
                'pending': pending, 'delivered_updates': delivered_updates}
    finally:
        _notification_drain_lock.release()


def _mapping_entry_facts(entry, facts_ts=0):
    entry = entry or {}
    episodes = set()
    for season in (entry or {}).get('seasons') or []:
        try:
            sn = int(season.get('season'))
        except (TypeError, ValueError):
            continue
        nums = season.get('episode_numbers')
        if nums is None:
            try:
                hi = int(season.get('max_ep') or 0)
            except (TypeError, ValueError):
                hi = 0
            missing = {int(x) for x in (season.get('missing') or []) if str(x).isdigit()}
            nums = [x for x in range(1, hi + 1) if x not in missing]
        for number in nums or []:
            try:
                en = int(number)
            except (TypeError, ValueError):
                continue
            if sn > 0 and en > 0:
                episodes.add((sn, en))
    return {
        'series_id': str((entry or {}).get('id') or ''),
        'series_ids': [str(x) for x in ((entry or {}).get('series_ids') or [(entry or {}).get('id')]) if x],
        'series_name': (entry or {}).get('name') or '',
        'poster': ((entry or {}).get('tmdb_info') or {}).get('poster') or (
            '/api/emby/poster/' + str(entry['id']) if entry.get('has_image') and entry.get('id') else ''),
        'year': (entry or {}).get('year'),
        'local_eps': (entry or {}).get('local_eps'),
        'share_eps': (entry or {}).get('share_eps'),
        'episodes': episodes,
        'source': 'library_facts',
        'facts_ts': (entry or {}).get('subscription_facts_ts') or facts_ts,
        'facts_version': (entry or {}).get('facts_version') or '',
    }


def _mapping_facts_by_tmdb(cache, only_tmdb_ids=None):
    by_tmdb = {}
    facts_ts = (cache or {}).get('facts_ts') or (cache or {}).get('ts') or 0
    for entry in (cache or {}).get('series') or []:
        tid = str(entry.get('tmdb_id') or '').strip()
        if not tid or (only_tmdb_ids is not None and tid not in only_tmdb_ids):
            continue
        facts = _mapping_entry_facts(entry, facts_ts)
        current = by_tmdb.setdefault(tid, facts)
        if current is not facts:
            current['episodes'] |= facts['episodes']
            current['series_ids'] = sorted(set(current['series_ids']) | set(facts['series_ids']))
            try:
                current['facts_ts'] = min(float(current.get('facts_ts') or 0), float(facts.get('facts_ts') or 0))
            except (TypeError, ValueError):
                current['facts_ts'] = 0
            if not current.get('series_name'):
                current['series_name'] = facts.get('series_name')
            # Duplicate Emby identities may overlap. Counts cannot be safely
            # summed without per-library episode sets; display them as unknown.
            current['local_eps'] = current['share_eps'] = None
            current['poster'] = current.get('poster') or facts.get('poster')
    return by_tmdb


def enqueue_ingest_notifications(mapping_result: dict) -> dict:
    """Schedule targeted subscription analysis after ingest fact persistence.

    Only changed mapping entries are handed to the worker.  The worker never
    enumerates unrelated subscriptions or performs a full Emby library query.
    """
    entries = list((mapping_result or {}).get('series_facts') or [])
    # Duplicate Emby Series IDs are unioned by TMDB identity.
    facts = {}
    for entry in entries:
        tid = str(entry.get('tmdb_id') or '').strip()
        if not tid:
            continue
        row = _mapping_entry_facts(entry, time.time())
        current = facts.setdefault(tid, row)
        if current is not row:
            current['episodes'] |= row['episodes']
            current['series_ids'] = sorted(set(current['series_ids']) | set(row['series_ids']))
            if not current.get('series_name'):
                current['series_name'] = row.get('series_name')
            current['local_eps'] = current['share_eps'] = None
            current['poster'] = current.get('poster') or row.get('poster')
    if not facts:
        return {'status': 'skipped', 'matched': 0, 'queued': 0, 'scheduled': False}
    ids = set(facts)

    # Queue synchronously after the mapping fact has been persisted.  The
    # queue write is local and bounded; Telegram delivery remains in the
    # scheduler's retry loop, so a process stop after ingest cannot lose the
    # event and the ingest task never waits on the network.
    try:
        result = check_subscriptions(send_notify=True, dispatch=False,
                                     only_tmdb_ids=ids, facts_by_tmdb=facts,
                                     fallback_missing=False, reason='ingest')
        return {'status': 'queued', 'matched': len(ids),
                'queued': len(result.get('updates') or []), 'scheduled': False,
                'tmdb_ids': sorted(ids), 'event_id': result.get('event_id')}
    except Exception as error:
        try:
            _mark_notification_reconcile()
        except Exception as storage_error:
            log.warning('追更补报标记保存失败，仍可通过手动/周期校准恢复: %s', storage_error)
        record_subscription_check_failure(error)
        log.warning('增量追更分析失败（不影响入库任务）: %s', error)
        return {'status': 'error', 'matched': len(ids), 'queued': 0, 'scheduled': False,
                'tmdb_ids': sorted(ids), 'error': str(error)}


def process_subscription_tmdb_calibration(*, dispatch=True) -> dict:
    """Low-frequency TMDB/aired reconciliation using persisted library facts."""
    cfg = _cfg.load_config()
    if cfg.get('subscribe_enabled', '1') != '1':
        return {'status': 'disabled', 'updates': [], 'checked': 0, 'failed': 0}
    try:
        from . import morning
    except ImportError:
        import morning
    ids = {str(s.get('tmdb_id') or '').strip() for s in _cfg.get_subscriptions()
           if s.get('enabled', True) and str(s.get('tmdb_id') or '').strip()}
    cache = (morning.read_emby_lib_cache(max_age=None) or {}) if ids else {}
    facts = _mapping_facts_by_tmdb(cache, only_tmdb_ids=ids)
    # A missing mapping fact is allowed to use the existing precise Emby
    # fallback during this six-hour calibration, but never in the five-minute
    # event path.
    return check_subscriptions(send_notify=True, only_tmdb_ids=ids or None,
                               facts_by_tmdb=facts, fallback_missing=True,
                               reason='tmdb_calibration', dispatch=dispatch)

def _subscription_report_file():
    return _eng().STATE_DIR / 'subscription_report.json'

def _save_subscription_report(updates):
    """保存最近一次真正成功推送的追更汇报。晨报只引用这份实际汇报。"""
    try:
        p = _subscription_report_file()
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {'ts': time.time(), 'date': time.strftime('%Y-%m-%d', time.localtime()), 'updates': updates or []}
        _state.save(p, payload)
    except (OSError, TypeError) as e:
        log.warning('保存追更实际汇报失败: %s', e)

def _subscription_check_status_file():
    return _eng().STATE_DIR / 'subscription_check_status.json'


def _save_subscription_check_status(status: str, **extra):
    """保存追更轮询可观测状态；失败不影响追更主流程。"""
    try:
        payload = {'status': str(status), 'ts': time.time(), **extra}
        p = _subscription_check_status_file()
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state.save(p, payload)
    except Exception:
        pass


def load_subscription_check_status():
    try:
        d = _state.read(_subscription_check_status_file())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record_subscription_check_failure(error):
    msg = str(error or '未知错误')
    _save_subscription_check_status('error', error=msg, updates=0)
    try:
        logger.write('追更', '后台检查失败', [f'原因 → {msg}'])
    except Exception:
        pass


def _load_subscription_report(today_only=False):
    try:
        data = _state.read(_subscription_report_file())
        if not isinstance(data, dict): return None
        if today_only and data.get('date') != time.strftime('%Y-%m-%d', time.localtime()): return None
        return data
    except (OSError, ValueError):
        return None

def _query_items(params):
    data = _eng().emby_request('/Items', params)
    if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
        raise RuntimeError('Emby 未返回有效 Items 列表')
    if int(data.get('TotalRecordCount') or 0) > len(data['Items']):
        raise RuntimeError('Emby 分页结果不完整')
    return data['Items']


def _emby_series_latest_ep(series_tmdb_id: str):
    """完整读取全部匹配副本；任一分集查询失败都不能返回部分成功。"""
    if not series_tmdb_id:
        return None
    base = {'Recursive': 'true', 'IncludeItemTypes': 'Series',
            'Fields': 'ProviderIds,Name,Path,ProductionYear,ImageTags', 'Limit': 50000}
    try:
        items = _query_items(dict(base, AnyProviderIdEquals=f'Tmdb.{series_tmdb_id}'))
    except Exception:
        items = []  # 精确搜索失败可以由一次完整的备用查询恢复。
    if not items:
        items = [it for it in _query_items(base)
                 if str((it.get('ProviderIds') or {}).get('Tmdb') or '') == str(series_tmdb_id)]
    if not items:
        return None
    have = set(); created = {}; series_ids = []; names = []
    for series in items:
        sid = series.get('Id')
        if not sid:
            raise RuntimeError('Emby Series 缺少 ID')
        series_ids.append(sid)
        if series.get('Name') and series['Name'] not in names:
            names.append(series['Name'])
        episodes = _query_items({'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                                 'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd,DateCreated,Path',
                                 'Limit': 50000})
        for ep in episodes:
            try:
                sn = int(ep.get('ParentIndexNumber') or 0)
                en = int(ep.get('IndexNumber') or 0)
                end = int(ep.get('IndexNumberEnd') or en)
            except (TypeError, ValueError):
                continue
            if sn <= 0 or en <= 0:
                continue
            for number in range(en, max(en, end) + 1):
                have.add((sn, number))
                created[(sn, number)] = max(created.get((sn, number), ''), ep.get('DateCreated') or '')
    sn, en = max(have) if have else (None, None)
    return {'series_id': series_ids[0], 'series_ids': series_ids,
            'poster': next(('/api/emby/poster/' + str(s['Id']) for s in items
                            if (s.get('ImageTags') or {}).get('Primary')), ''),
            'year': items[0].get('ProductionYear'),
            'series_name': names[0] if names else '', 'season': sn, 'episode': en,
            'date_created': created.get((sn, en), ''), 'episodes': have, 'source': 'emby_live'}

def _disk_series_eps(series_tmdb_id: str):
    """追更磁盘兜底：按目录名 tmdb 定位剧集分集（不依赖 Emby ProviderIds.Tmdb）。

    上游 TgtoDrive 把 tmdb 写在目录名 `{tmdb-xxx}` 里，而 Emby 的刮削器不会把
    目录名里的 tmdb 后缀解析进 ProviderIds，导致 ``_emby_series_latest_ep`` 按
    ``ProviderIds.Tmdb`` 查不到 → 追更拿不到任何分集、「不能用」。
    这里直接扫描两库磁盘，复用 _eng().Lib 已经建好的 `tmdb_refs['tv:<id>']` 索引，
    按目录名 tmdb 匹配并 union 分集。返回结构与 ``_emby_series_latest_ep`` 一致，
    多一个 ``source='disk'`` 便于日志区分。
    """
    if not series_tmdb_id:
        return None
    eps = set()
    names = []
    try:
        for lib in (_eng()._get_lib(_eng().L_ROOT), _eng()._get_lib(_eng().S_ROOT)):
            for key in lib.tmdb_refs.get('tv:' + str(series_tmdb_id), ()):
                disp = lib.meta.get(key, ('', '', None))[0]
                if disp and disp not in names:
                    names.append(disp)
                for sn, files in lib.tv.get(key, {}).items():
                    if sn <= 0:
                        continue
                    for f in files:
                        try:
                            ep = get_ep(f.name, f.parent.name, allow_bare_ep=True)
                        except Exception:
                            continue
                        if ep and ep[0] > 0 and ep[1] > 0:
                            eps.add(ep)
    except Exception as e:
        log.warning('追更磁盘兜底扫描失败 %s: %s', series_tmdb_id, e)
        raise RuntimeError(f'磁盘分集读取失败: {e}') from e
    sn, en = max(eps) if eps else (None, None)
    return {
        'series_id': None, 'series_ids': [],
        'series_name': names[0] if names else '',
        'season': sn, 'episode': en,
        'date_created': '', 'episodes': eps, 'source': 'disk',
    }

def _ep_key_num(k):
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)) * 10000 + int(m.group(2))) if m else 0

def _ep_key(sn, en):
    """(1, 7) -> 'S01E07'"""
    return f'S{int(sn):02d}E{int(en):02d}'

def _parse_ep_key(k):
    """'S01E07' -> (1, 7)；解析不了返回 None"""
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)), int(m.group(2))) if m else None

def _eps_to_keys(eps):
    """[(季,集)] -> {'S01E01', ...}"""
    return {_ep_key(sn, en) for sn, en in (eps or []) if sn > 0 and en > 0}

def _keys_to_eps(keys):
    """{'S01E01', ...} -> {(季,集)}（忽略解析不出的字符串）"""
    out = set()
    for k in (keys or []):
        p = _parse_ep_key(k)
        if p:
            out.add(p)
    return out

def _fmt_ep_ranges(eps):
    """把集列表格式化成人类可读的区间串。

    同一季内连续集号合并：[(1,16),(1,17),(1,18)] -> 'S01E16–E18'
    单集保留完整写法：[(1,16)] -> 'S01E16'
    跨季用 ', ' 连接：[(1,10),(2,1),(2,2)] -> 'S01E10, S02E01–E02'
    集数较多时折叠尾部，避免 Telegram 消息被撑爆。
    """
    items = sorted({(int(sn), int(en)) for sn, en in (eps or []) if sn > 0 and en > 0})
    if not items:
        return ''
    parts = []
    i, n = 0, len(items)
    while i < n:
        sn, lo = items[i]
        hi = lo
        j = i + 1
        # 只在同季内合并连续集号；跨季一定断开（S01E10 后面不是 S01E11 的延续）
        while j < n and items[j][0] == sn and items[j][1] == hi + 1:
            hi = items[j][1]
            j += 1
        parts.append(_ep_key(sn, lo) if hi == lo else f'S{sn:02d}E{lo:02d}–E{hi:02d}')
        i = j
    if len(parts) > 6:
        parts = parts[:6] + [f'…等 {n} 集']
    return ', '.join(parts)

def _precise_calibration_facts(tmdb_id, cached):
    """Bounded targeted API reads only; no full Series or disk-scan fallback."""
    ids = list(dict.fromkeys(str(x) for x in (cached or {}).get('series_ids') or [] if x))
    if len(ids) > 64:
        raise RuntimeError('匹配副本数量异常，保留旧状态')
    params = {'Recursive': 'true', 'IncludeItemTypes': 'Series',
              'Fields': 'ProviderIds,Name,Path', 'Limit': 64}
    if ids:
        params['Ids'] = ','.join(ids)
    else:
        params['AnyProviderIdEquals'] = 'Tmdb.' + str(tmdb_id)
    rows = _query_items(params)
    if not rows or (ids and {str(x.get('Id') or '') for x in rows} != set(ids)):
        raise RuntimeError('精准校准未取得全部匹配副本，保留旧状态')
    if any(not x.get('Id') or x.get('Type') != 'Series' or
           str((x.get('ProviderIds') or {}).get('Tmdb') or '') != str(tmdb_id) for x in rows):
        raise RuntimeError('精准校准身份不符，保留旧状态')
    have = set()
    for row in rows:
        episodes = _query_items({'ParentId': str(row['Id']), 'Recursive': 'true',
                                'IncludeItemTypes': 'Episode', 'Limit': 5000,
                                'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd'})
        for ep in episodes:
            try:
                sn, en = int(ep.get('ParentIndexNumber') or 0), int(ep.get('IndexNumber') or 0)
                end = max(en, int(ep.get('IndexNumberEnd') or en))
            except (TypeError, ValueError):
                continue
            if sn > 0 and en > 0:
                if end - en > 1000:
                    raise RuntimeError('分集区间异常，保留旧状态')
                have.update((sn, number) for number in range(en, end + 1))
    return {'series_ids': [str(row['Id']) for row in rows],
            'series_name': rows[0].get('Name') or '', 'episodes': have,
            'source': 'emby_live', 'facts_ts': time.time()}


def _prepare_subscription_observations(only_tmdb_ids, facts_by_tmdb, fallback_missing, reason):
    """Network/disk observations outside the short state/queue critical section."""
    cfg = _cfg.load_config()
    if cfg.get('subscribe_enabled', '1') != '1':
        return {}
    allowed = {str(x) for x in only_tmdb_ids} if only_tmdb_ids is not None else None
    try:
        freshness = max(900, int(cfg.get('ingest_interval_min') or 5) * 120)
    except (TypeError, ValueError):
        freshness = 900
    observed = {}
    for sub in _cfg.get_subscriptions():
        tid = str(sub.get('tmdb_id') or '')
        if not tid or not sub.get('enabled', True) or tid in observed or (allowed is not None and tid not in allowed):
            continue
        item = {'latest': copy.deepcopy((facts_by_tmdb or {}).get(tid)),
                'fallback_reason': '', 'error': '', 'tmdb_info': None, 'tmdb_error': ''}
        observed[tid] = item
        latest = item['latest']
        if latest is None and facts_by_tmdb is not None and not fallback_missing:
            item['skip'] = True
            continue
        try:
            stamp = float((latest or {}).get('facts_ts') or 0)
        except (TypeError, ValueError):
            stamp = 0
        stale = not stamp or time.time() - stamp > freshness or stamp > time.time() + 60
        if reason == 'tmdb_calibration' and (latest is None or stale):
            try:
                item['latest'] = _precise_calibration_facts(tid, latest)
            except Exception as error:
                item['error'] = str(error)
                continue  # never infer absence from stale/unavailable data
        elif latest is None:
            try:
                item['latest'] = _emby_series_latest_ep(sub['tmdb_id'])
            except Exception as error:
                item['fallback_reason'] = str(error)
            if item['latest'] is None:
                try:
                    item['latest'] = _disk_series_eps(sub['tmdb_id'])
                    if item['latest'] is None:
                        raise RuntimeError('未取得完整分集事实')
                except Exception as error:
                    item['error'] = '; '.join(filter(None, [item['fallback_reason'], str(error)]))
                    continue
        item['latest'].setdefault('facts_ts', time.time())
        if cfg.get('subscribe_check_tmdb', '1') == '1':
            try:
                item['tmdb_info'] = _eng()._tmdb_series_info(sub['tmdb_id'])
                if item['tmdb_info'] is None:
                    raise RuntimeError('TMDB 对照不可用，保留上次缺集记录')
            except Exception as error:
                item['tmdb_error'] = str(error)
    return observed


def check_subscriptions(send_notify=True, *, only_tmdb_ids=None, facts_by_tmdb=None,
                        fallback_missing=True, reason='manual', dispatch=True) -> dict:
    if reason == 'ingest':
        fallback_missing = False
        facts_by_tmdb = facts_by_tmdb if facts_by_tmdb is not None else {}
    try:
        from .tmdb import subscription_tmdb_session
    except ImportError:
        from tmdb import subscription_tmdb_session
    with subscription_tmdb_session(force_refresh=(reason == 'tmdb_calibration')):
        observations = None
        if reason != 'ingest':
            # Only one expensive observation pass; ingest never takes this
            # lock and can journal new facts while TMDB/Emby are slow.
            with _subscription_observation_lock:
                observations = _prepare_subscription_observations(
                    only_tmdb_ids, facts_by_tmdb, fallback_missing, reason)
        try:
            with _subscription_check_lock:
                result = _check_subscriptions(
                    send_notify=send_notify, only_tmdb_ids=only_tmdb_ids,
                    facts_by_tmdb=facts_by_tmdb, fallback_missing=fallback_missing,
                    reason=reason, observations=observations)
        except NotificationBackpressure as error:
            record_subscription_check_failure(error)
            raise
        # Synchronous manual/API checks retain their existing return contract,
        # but network delivery must not own the state/ingest check lock.
        if send_notify and dispatch and result.get('status') != 'disabled':
            delivery = drain_subscription_notifications(force=True)
            if reason == 'manual' and not result.get('updates') and delivery.get('sent'):
                result['updates'] = delivery.get('delivered_updates') or []
            event_id = result.get('event_id')
            with _notification_lock:
                queue = _load_notification_queue()
                event = next((e for e in queue.get('events') or [] if e.get('id') == event_id), None)
                result['notification_pending'] = any(e.get('status') != 'sent' for e in queue.get('events') or [])
            result['sent'] = (bool(event and event.get('status') == 'sent') if event_id
                              else (True if delivery.get('sent') else None))
            if delivery.get('failed'):
                result['status'] = 'error'
                result['error'] = delivery.get('error') or 'Telegram 推送失败'
            _save_subscription_check_status(
                result['status'], finished_at=time.time(), updates=len(result.get('updates') or []),
                **{k: result.get(k) for k in ('total', 'checked', 'paused', 'failed', 'rows', 'sent', 'error', 'reason')})
        return result


def _check_subscriptions(send_notify=True, *, only_tmdb_ids=None, facts_by_tmdb=None,
                         fallback_missing=True, reason='manual',
                         observations=None) -> dict:
    """追更订阅检查（集合差集状态机）。

    核心口径（修复「吞通知 / 重复通知 / 跳集不报警」）：

    1. **集合差集**：每次检查取 Emby 实际存在的集 ``have``，与状态文件里
       ``notified_episodes``（已成功通知过的集）求差 → ``new_eps``；与 TMDB 已播集
       求差 → ``missing_eps``。不再只看「最大集号」，所以中间插入的集也能被发现。
    2. **发送成功才记账**：``notify_telegram()`` 返回 True 后才把 ``new_eps`` 并入
       ``notified_episodes``。发送失败则状态原样不动，下一轮自动重试 —— 既不丢通知
       也不重复刷屏。
    3. **跳集报警**：``newly_missing``（本次新出现的缺集）与 ``refilled``（本次补齐的
       缺集）分别通知，提示精确到集号区间。
    4. **免迁移**：首次遇到只有旧 ``latest_ep`` 的订阅时，把它当成「已通知过的最后一集」
       向前播种，避免升级后把全库旧集刷一遍。
    """
    started = time.time()
    _save_subscription_check_status('running', started_at=started, updates=0)
    cfg = _cfg.load_config()
    if cfg.get('subscribe_enabled', '1') != '1':
        result = {'status': 'disabled', 'updates': [], 'skipped': 'disabled', 'checked': 0, 'failed': 0, 'rows': []}
        _save_subscription_check_status('disabled', finished_at=time.time(), duration_ms=int((time.time()-started)*1000), updates=0)
        return result
    subs = _cfg.get_subscriptions()
    if not subs:
        result = {'status': 'success', 'updates': [], 'total': 0, 'checked': 0, 'failed': 0, 'rows': []}
        _save_subscription_check_status('success', finished_at=time.time(), duration_ms=int((time.time()-started)*1000), updates=0, total=0, sent=None)
        return result

    check_tmdb = cfg.get('subscribe_check_tmdb', '1') == '1'
    # An ingest event reuses the last reliable TMDB aired baseline.  TMDB is
    # refreshed by the separate low-frequency calibration, never by every
    # five-minute ingest tick.
    if reason == 'ingest':
        check_tmdb = False
    only_ids = {str(x) for x in only_tmdb_ids} if only_tmdb_ids is not None else None
    state = _load_sub_state()
    queued_claims = _pending_claims_snapshot() if send_notify else {}
    updates = []
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 记录每个订阅本轮「发送成功后应写回」的状态，发送前不落盘
    staged = {}
    rows = []     # 本轮每部订阅的检查结果（写进执行记录，点开能看到具体是哪部剧、哪几集）

    paused = 0
    failed = 0
    checked = 0
    for sub in subs:
        if not sub.get('enabled', True):
            paused += 1          # 已暂停：不检查，但要在状态里如实报告，避免「检查 1 部」却什么都没检查
            continue
        sid = sub.get('id') or sub.get('tmdb_id') or sub.get('name')
        tmdb_id = sub.get('tmdb_id')
        if only_ids is not None and str(tmdb_id or '') not in only_ids:
            continue
        if not tmdb_id:
            failed += 1
            rows.append({'sid': sid, 'name': sub.get('name'), 'status': 'error',
                         'changed': False, 'error': '未配置 TMDB ID'})
            continue
        errors = []; fallback_reason = ''
        facts_ts = 0
        observation = (observations or {}).get(str(tmdb_id)) if observations is not None else None
        if observations is not None:
            if observation is None or observation.get('error'):
                failed += 1
                rows.append({'sid': sid, 'name': sub.get('name'), 'tmdb_id': tmdb_id,
                             'status': 'error', 'changed': False,
                             'error': (observation or {}).get('error') or '订阅已变化，等待下轮核实'})
                continue
            if observation.get('skip'):
                rows.append({'sid': sid, 'name': sub.get('name'), 'tmdb_id': tmdb_id,
                             'status': 'skipped', 'changed': False, 'error': '片库事实未匹配，等待校准'})
                continue
            latest = copy.deepcopy(observation['latest'])
            fallback_reason = observation.get('fallback_reason') or ''
            facts_ts = latest.get('facts_ts') or 0
            accepted_ts = max(float((state.get(sid) or {}).get('facts_ts') or 0),
                              float((queued_claims.get(str(sid)) or {}).get('facts_ts') or 0))
            if float(facts_ts) < accepted_ts:
                rows.append({'sid': sid, 'name': sub.get('name'), 'tmdb_id': tmdb_id,
                             'status': 'skipped', 'changed': False,
                             'error': '观察期间已取得更新事实，保留最新状态，等待下轮核实'})
                continue
        elif facts_by_tmdb is not None:
            latest = copy.deepcopy(facts_by_tmdb.get(str(tmdb_id)))
            facts_ts = (latest or {}).get('facts_ts') or 0
            if latest is None and not fallback_missing:
                # The event must not manufacture facts from an incomplete
                # mapping.  The next six-hour calibration/manual check can
                # recover it with a precise fallback query.
                rows.append({'sid': sid, 'name': sub.get('name'), 'tmdb_id': tmdb_id,
                             'status': 'skipped', 'changed': False,
                             'error': '片库事实未匹配，等待校准'})
                continue
        else:
            latest = None
        if latest is None:
            try:
                latest = _emby_series_latest_ep(tmdb_id)
            except Exception as error:
                latest = None
                fallback_reason = str(error)
            if latest is None:
                try:
                    latest = _disk_series_eps(tmdb_id)
                    if latest is None:
                        raise RuntimeError('未取得完整分集事实')
                except Exception as error:
                    failed += 1
                    rows.append({'sid': sid, 'name': sub.get('name'), 'tmdb_id': tmdb_id,
                                 'status': 'error', 'changed': False,
                                 'error': '; '.join(filter(None, [fallback_reason, str(error)]))})
                    continue
        checked += 1
        tmdb_info = None
        if check_tmdb:
            try:
                if observation is not None:
                    if observation.get('tmdb_error'):
                        raise RuntimeError(observation['tmdb_error'])
                    tmdb_info = observation.get('tmdb_info')
                else:
                    tmdb_info = _eng()._tmdb_series_info(tmdb_id)
                if tmdb_info is None:
                    raise RuntimeError('TMDB 对照不可用，保留上次缺集记录')
            except Exception as error:
                errors.append(str(error))
                failed += 1
        prev = state.get(sid) or {}
        have = set(latest.get('episodes') or [])
        facts_ts = facts_ts or time.time()
        facts_version = hashlib.sha256(json.dumps(sorted(have)).encode()).hexdigest()[:16]

        # ── 免迁移播种 ──
        # 老结构只有 latest_ep：把它当成「已通知过的最后一集」，该季 1..N 全部视为
        # 已通知，避免升级后一次刷出几十条历史集。全新订阅则把当前已有的集全部播种，
        # 首次检查保持安静（避免「刚加订阅就被刷屏」）。
        prev_notified_raw = prev.get('notified_episodes')
        if prev_notified_raw is None:
            prev_latest = prev.get('latest_ep') or ''
            seed = set()
            lp = _parse_ep_key(prev_latest)
            if lp:
                if have:
                    # 只播种 latest_ep 所属季；其它季由后续轮次自然发现
                    seed |= {(sn, en) for (sn, en) in have if sn == lp[0] and en <= lp[1]}
                else:
                    # Emby 查不到分集时保守播种到 latest_ep（假设中间无断层）
                    seed |= {(lp[0], e) for e in range(1, lp[1] + 1)}
            else:
                seed = set(have)          # 全新订阅：当前已有的都算已知
            notified = seed
            migrated = True
        else:
            notified = _keys_to_eps(prev_notified_raw)
            migrated = False

        prev_missing = _keys_to_eps(prev.get('missing_episodes'))

        # A failed or not-yet-committed Telegram batch already owns its
        # episode keys.  These are not available for a subsequent 5m batch.
        claimed = queued_claims.get(str(sid)) or {}
        claimed_new = _keys_to_eps(claimed.get('new_eps'))
        claimed_missing = _keys_to_eps(claimed.get('newly_missing'))
        new_eps = sorted(have - notified - claimed_new)          # 真实新增、且未排队
        # TMDB 不可用/停用时不推断新缺集，仍可确认原缺集已经入库。
        missing_eps = prev_missing - have
        if tmdb_info is not None:
            aired = tmdb_info.get('aired') or set()
            # ── 缺集只认「从未入库过的集」──
            # 若只算 aired - have，那么「曾经有、后来被删」的集会被误报成缺集
            # （下架 / 洗版删旧时最容易遇到）。取 aired - (have | notified)：
            # 已通知过的集视为「到过库」，即使当下不在也不算缺集。
            missing_eps = aired - have - notified - claimed_new
        newly_missing = sorted(missing_eps - prev_missing - claimed_missing)  # 未排队的新缺集
        refilled = sorted(set(new_eps) & prev_missing)          # 本次被补齐的缺集

        # 通知只由「状态相对上次的变化」驱动，不看绝对值
        has_change = bool(new_eps or newly_missing or refilled)

        # 新基线：notified_episodes 只在发送成功后才并入 new_eps，所以这里先按
        # 「已通知集 + 本轮新增」算出候选值，发送失败时回退到旧 notified。
        st = {
            **prev,
            'tmdb_id': tmdb_id,
            'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
            'notified_episodes': sorted(_eps_to_keys(notified | set(new_eps))),
            'missing_episodes': sorted(_eps_to_keys(missing_eps)),
            'tmdb_total': (tmdb_info or {}).get('total_episodes', prev.get('tmdb_total') or 0),
            'tmdb_declared': (tmdb_info or {}).get('declared_total', prev.get('tmdb_declared') or 0),
            'tmdb_status': (tmdb_info or {}).get('status', prev.get('tmdb_status', '')),
            'observed_have': len(have), 'facts_ts': facts_ts, 'facts_version': facts_version,
            'facts_source': latest.get('source') or 'emby_live',
            'tmdb_source': (tmdb_info or {}).get('source') or ('tmdb' if tmdb_info else ('unavailable' if check_tmdb else 'disabled')),
            'tmdb_ts': (tmdb_info or {}).get('ts'), 'updated_at': now_str,
        }
        # notified_episodes 为空时保持旧 latest_ep，避免 Emby 短暂异常把展示清空
        all_keys = st['notified_episodes']
        st['latest_ep'] = max(all_keys, key=_ep_key_num) if all_keys else (prev.get('latest_ep') or '')
        staged[sid] = st
        rows.append({
            'sid': sid, 'name': st['name'], 'tmdb_id': tmdb_id, 'changed': has_change,
            'status': 'partial' if errors else 'success', 'error': '; '.join(errors),
            'facts_source': st['facts_source'], 'facts_ts': facts_ts, 'facts_version': facts_version,
            'fallback_reason': fallback_reason, 'tmdb_source': st['tmdb_source'], 'tmdb_ts': st['tmdb_ts'],
            'latest_ep': st['latest_ep'], 'have': len(have),
            'tmdb_total': st['tmdb_total'], 'tmdb_declared': st['tmdb_declared'],
            'tmdb_status': st['tmdb_status'],
            'new': _fmt_ep_ranges(new_eps), 'refilled': _fmt_ep_ranges(refilled),
            'newly_missing': _fmt_ep_ranges(newly_missing),
            'missing': len(missing_eps), 'missing_eps': _fmt_ep_ranges(missing_eps),
        })

        if has_change:
            updates.append({
                'sid': sid,
                'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
                'tmdb_id': tmdb_id,
                'poster': latest.get('poster') or sub.get('poster') or (tmdb_info or {}).get('poster') or '',
                'year': latest.get('year') or sub.get('year') or (tmdb_info or {}).get('year'),
                'have': len(have), 'tmdb_total': (tmdb_info or {}).get('total_episodes'),
                'tmdb_source': st['tmdb_source'], 'facts_source': st['facts_source'],
                'local_eps': latest.get('local_eps'), 'share_eps': latest.get('share_eps'),
                'season_counts': [{'season': sn, 'have': sum(1 for ss, _ in have if ss == sn)}
                                  for sn in sorted({sn for sn, _ in set(new_eps) | set(newly_missing) | set(refilled)})],
                'new_eps': sorted(_eps_to_keys(new_eps)),
                'missing_eps': sorted(_eps_to_keys(missing_eps)),
                'newly_missing': sorted(_eps_to_keys(newly_missing)),
                'refilled': sorted(_eps_to_keys(refilled)),
            })

    # ── 发送成功才记账 ──
    # send_notify=False（bot/保存订阅的静默校验）等价于已确认，不产生
    # Telegram 事件；普通增量通知则先持久化队列，再由 dispatcher 发送。
    event = None
    if send_notify and updates:
        update_sids = {str(u.get('sid') or '') for u in updates}
        _persist_subscription_snapshots({sid: st for sid, st in staged.items()
                                         if str(sid) not in update_sids})
        # Immutable UUID batches and _notification_lock protect commit/claims.
        # Do not acquire the network sender lock here: ingest must never wait
        # for an in-flight Telegram call.
        event = _queue_update_batch(updates, staged, reason=reason)
    else:
        _persist_subscription_snapshots(staged)
    with _notification_lock:
        pending = any(e.get('status') != 'sent' for e in _load_notification_queue().get('events') or []) if send_notify else False
    outcome = 'error' if failed and not checked else ('partial' if failed else 'success')
    error_text = '部分订阅读取失败，请查看逐部原因' if failed else ''
    result = {'status': outcome, 'updates': updates, 'total': len(subs), 'checked': checked, 'paused': paused,
              'failed': failed, 'rows': rows, 'error': error_text,
              'sent': False if send_notify and updates else None,
              'event_id': (event or {}).get('id') if event else None,
              'notification_pending': pending,
              'reason': reason}
    _save_subscription_check_status(
        outcome, finished_at=time.time(), duration_ms=int((time.time()-started)*1000),
        updates=len(updates), total=len(subs), checked=checked, paused=paused,
        failed=failed, rows=rows, sent=result.get('sent'), error=error_text, reason=reason,
    )
    try:
        summary = [f'检查 {checked} 部订阅' + (f'（{paused} 部已暂停，未检查）' if paused else ''),
                   f'发现 {len(updates)} 部有变化', f'{failed} 部读取失败/不完整']
        if send_notify and updates:
            summary.append('通知已持久化，等待调度发送')
        logger.write('追更', '订阅检查', summary,
                     extra={'shows': rows, 'checked': checked, 'paused': paused, 'failed': failed,
                            'changed': len(updates),
                            'sent': result.get('sent'),
                            'reason': reason})
    except Exception:
        pass
    return result
