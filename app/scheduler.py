# -*- coding: utf-8 -*-
"""后台调度：入库缓存刷新 / TMDB 对照预热 / 追更订阅 / 双库定时巡检 / 晨报。

以前这些逻辑写在 main.py 里、导入模块就起线程；现在由 Web 应用的生命周期
（FastAPI lifespan）显式 start()/stop()，Web 层只负责 HTTP。
"""
if __package__:
    from . import state_store as _state
else:
    import state_store as _state
import hashlib
import json
import threading
import time
from argparse import Namespace

from app import engine, bot, tasks
from app import config as _cfg

TICK_SEC = 30
STARTUP_DELAY_SEC = 15  # 启动后缓 15 秒，等 Emby 就绪
TMDB_RETRY_SEC = 1800

_stop = threading.Event()
_thread = {'t': None}
_bg_state = {
    'last_ingest_check': 0,
    'last_sub_check': 0,
    'last_sub_attempt': 0,
    'sub_running': False,
    'last_sub_result': None,
    'last_tmdb_attempt': 0,
}


# ═══════════════════ 双库治理：定时巡检（只扫描+通知，不自动清理） ═══════════════════
GOV_AUTO_FILE = engine.DATA_DIR / 'gov_auto.json'
_GOV_AUTO_DEFAULT = {
    'enabled': False,
    'interval_hours': 6,
    'notify_when_clean': False,
    'only_on_change': True,
    'last_run': 0,
    'last_status': '',
    'last_sig': '',
}
_gov_auto_lock = threading.Lock()
_gov_auto_running = {'v': False}


def gov_auto_load() -> dict:
    d = dict(_GOV_AUTO_DEFAULT)
    d.update(_state.read(GOV_AUTO_FILE, {}))
    return d


def gov_auto_save(d: dict):
    with _gov_auto_lock:
        _state.save(GOV_AUTO_FILE, d)


def _gov_auto_sig(res: dict) -> str:
    keys = []
    for k in ('del_local_items', 'del_share_items'):
        for it in res.get(k) or []:
            keys.append('%s|%s|%s' % (k, it.get('title', ''), it.get('text', '')))
    keys.sort()
    return hashlib.sha1('\n'.join(keys).encode('utf-8')).hexdigest()


def _gov_auto_run():
    """后台线程：执行一次静默扫描，按设置决定是否推送 Telegram。"""
    try:
        try:
            t = tasks.manager.spawn('inter_check', engine.ACTIONS['inter_check'],
                                    Namespace(kw='', plan='', dry_run=False, silent=True),
                                    source='schedule')
        except tasks.TaskBusy:
            engine.log.info('治理巡检：已有任务在执行，稍后重试')
            return
        t.done.wait()
        cfg = gov_auto_load()
        cfg['last_run'] = time.time()
        if t.status != 'success' or not isinstance(t.result, dict):
            cfg['last_status'] = '扫描失败: %s' % (t.error or '未知错误')
            gov_auto_save(cfg)
            bot.notify_auto_scan(None, error=cfg['last_status'])
            return
        res = t.result
        total = res.get('total_clean_cnt', 0)
        sig = _gov_auto_sig(res) if total else ''
        cfg['last_status'] = '待清理本地 %d / 待淘汰分享 %d' % (res.get('del_local_cnt', 0), res.get('del_share_cnt', 0))
        should = False
        if total > 0:
            should = (not cfg.get('only_on_change')) or sig != cfg.get('last_sig', '')
        elif cfg.get('notify_when_clean'):
            should = True
        if should and bot.notify_auto_scan(res):
            cfg['last_sig'] = sig
        elif total == 0:
            cfg['last_sig'] = ''
        gov_auto_save(cfg)
    except Exception as e:
        engine.log.warning('治理巡检异常: %s', e)
    finally:
        _gov_auto_running['v'] = False


def _gov_auto_tick(now: float):
    if _gov_auto_running['v']:
        return
    cfg = gov_auto_load()
    if not cfg.get('enabled'):
        return
    try:
        interval = max(1, min(168, int(cfg.get('interval_hours') or 6))) * 3600
    except (ValueError, TypeError):
        interval = 6 * 3600
    if now - float(cfg.get('last_run') or 0) < interval:
        return
    _gov_auto_running['v'] = True
    threading.Thread(target=_gov_auto_run, daemon=True, name='gov-auto').start()


def gov_auto_view() -> dict:
    d = gov_auto_load()
    last = float(d.get('last_run') or 0)
    interval = max(1, int(d.get('interval_hours') or 6)) * 3600
    return {
        'enabled': bool(d['enabled']),
        'interval_hours': int(d['interval_hours']),
        'notify_when_clean': bool(d['notify_when_clean']),
        'only_on_change': bool(d['only_on_change']),
        'last_run': last,
        'last_status': d.get('last_status', ''),
        'next_run': (last + interval) if (d['enabled'] and last) else 0,
        'running': _gov_auto_running['v'],
    }


def gov_auto_update(body: dict) -> dict:
    d = gov_auto_load()
    if 'enabled' in body:
        d['enabled'] = bool(body['enabled'])
    if 'interval_hours' in body:
        try:
            d['interval_hours'] = max(1, min(168, int(body['interval_hours'])))
        except (ValueError, TypeError):
            d['interval_hours'] = 6
    if 'notify_when_clean' in body:
        d['notify_when_clean'] = bool(body['notify_when_clean'])
    if 'only_on_change' in body:
        d['only_on_change'] = bool(body['only_on_change'])
    gov_auto_save(d)
    return gov_auto_view()


# ═══════════════════ 主循环 ═══════════════════
def _tick(now: float):
    """每 30 秒一次，决定是否触发：入库缓存刷新 / 订阅检查 / 巡检 / 晨报预扫 / 晨报发送"""
    cfg = _cfg.load_config()

    # ── 入库监控（默认 5 分钟） ──
    try:
        ingest_enabled = cfg.get('ingest_enabled', '1') == '1'
        ingest_interval = max(60, int(cfg.get('ingest_interval_min') or 5) * 60)
    except (ValueError, TypeError):
        ingest_enabled = True; ingest_interval = 300
    if ingest_enabled and (now - _bg_state['last_ingest_check']) >= ingest_interval:
        _bg_state['last_ingest_check'] = now
        try:
            engine.refresh_ingest_cache()
        except Exception as e:
            engine.log.warning('入库缓存刷新失败: %s', e)

    # ── TMDB 对照（后台预热） ──
    try:
        scan_enabled = cfg.get('tmdb_scan_enabled', '1') == '1'
        scan_hours = int(cfg.get('tmdb_scan_interval_hours') or 24)
        last_ts = float(cfg.get('tmdb_scan_last_ts') or '0')
    except (ValueError, TypeError):
        scan_enabled = True; scan_hours = 24; last_ts = 0
    # 全量对照可能要跑几十分钟：放到独立线程，不能堵住同一循环里的订阅检查和晨报；
    # 失败时最多 30 分钟重试一次，避免 Emby/TMDB 不可用时每 30 秒刷一遍
    if scan_enabled and (now - last_ts) >= scan_hours * 3600 \
            and now - _bg_state['last_tmdb_attempt'] >= TMDB_RETRY_SEC \
            and not engine.get_tmdb_scan_progress().get('running'):
        _bg_state['last_tmdb_attempt'] = now
        threading.Thread(target=engine.refresh_tmdb_scan, daemon=True, name='tmdb-scan').start()

    # ── 订阅轮询 ──
    try:
        interval = max(300, int(cfg.get('subscribe_interval_min') or 30) * 60)
    except ValueError:
        interval = 1800
    if cfg.get('subscribe_enabled', '1') == '1' and not _bg_state['sub_running'] \
            and (now - _bg_state['last_sub_check']) >= interval:
        _bg_state['sub_running'] = True
        _bg_state['last_sub_attempt'] = now
        try:
            engine.log.info('追更订阅轮询开始')
            result = engine.check_subscriptions(send_notify=True)
            _bg_state['last_sub_result'] = result
            _bg_state['last_sub_check'] = now
        except Exception as e:
            # 失败不等整个订阅周期；5 分钟后重试，且在前端留下明确诊断。
            engine.log.warning('订阅检查失败: %s', e)
            try: engine.record_subscription_check_failure(e)
            except Exception: pass
            _bg_state['last_sub_check'] = max(0, now - interval + 300)
        finally:
            _bg_state['sub_running'] = False

    # ── 双库治理定时巡检 ──
    try:
        _gov_auto_tick(now)
    except Exception as e:
        engine.log.warning('治理巡检调度失败: %s', e)

    # ── 晨报预扫 + 发送 ──
    try:
        mr = _cfg.get_morning_report()
    except Exception:
        mr = {'enabled': False}
    if not mr.get('enabled'):
        return
    lt = time.localtime()
    today = time.strftime('%Y-%m-%d', lt)
    hour = int(mr.get('hour', 9)); minute = int(mr.get('minute', 0))
    prescan_min = max(0, int(mr.get('prescan_min', 5)))
    lt_minutes = lt.tm_hour * 60 + lt.tm_min
    target_minutes = hour * 60 + minute

    # ── 预扫：提前 N 分钟静默刷新入库缓存 ──
    if prescan_min > 0:
        # 使用环形分钟差，正确处理 00:xx 的跨午夜窗口。
        minutes_until_target = (target_minutes - lt_minutes) % (24 * 60)
        in_prescan_window = 0 < minutes_until_target <= prescan_min
        if in_prescan_window and mr.get('prescan_last_date') != today:
            engine.log.info('晨报预扫触发')
            try:
                engine.refresh_ingest_cache()
                # 晨报前预扫同时刷新 Emby 缺集快照；9:00 只引用这次成功扫描的结果。
                try:
                    engine.gap_report(max_age=None, force_refresh=True, cache_only=False)
                except Exception as e:
                    engine.log.warning('晨报 Emby 缺集预扫失败: %s', e)
                _cfg.mark_morning_prescan(today)
            except Exception as e:
                engine.log.warning('晨报预扫失败: %s', e)

    # ── 到点发送 ──
    if lt_minutes >= target_minutes and mr.get('last_date') != today:
        try:
            ok = engine.send_morning_report(mr.get('items') or [], force_refresh=False, mark_sent=True)
            if ok:
                engine.log.info('晨报已发送')
        except Exception as e:
            engine.log.warning('晨报发送失败: %s', e)


def _loop():
    if _stop.wait(STARTUP_DELAY_SEC):
        return
    while not _stop.is_set():
        try:
            _tick(time.time())
        except Exception as e:
            engine.log.warning('后台轮询异常: %s', e)
        _stop.wait(TICK_SEC)


def start():
    t = _thread['t']
    if t is not None and t.is_alive():
        if not _stop.is_set():
            return
        t.join(TICK_SEC + 5)  # 上一轮 stop() 后旧线程还没退出：等它退出再起新的，否则会一个都不剩
    _stop.clear()
    t = threading.Thread(target=_loop, daemon=True, name='bg-poller')
    t.start()
    _thread['t'] = t


def stop(timeout=5):
    _stop.set()
    t = _thread['t']
    if t is not None and t is not threading.current_thread():
        t.join(timeout)
