# -*- coding: utf-8 -*-
"""从 engine.py 拆出。跨层符号统一经 _eng() 惰性访问（monkeypatch 穿透 + 双导入兼容）。"""
if __package__:
    from . import state_store as _state
else:
    import state_store as _state
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

log = logging.getLogger('strimkeep')

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
            'Fields': 'ProviderIds,Name,Path', 'Limit': 50000}
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

def check_subscriptions(send_notify=True) -> dict:
    try:
        from .tmdb import subscription_tmdb_session
    except ImportError:
        from tmdb import subscription_tmdb_session
    with subscription_tmdb_session():
        return _check_subscriptions(send_notify=send_notify)


def _check_subscriptions(send_notify=True) -> dict:
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
    state = _load_sub_state()
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
        if not tmdb_id:
            failed += 1
            rows.append({'sid': sid, 'name': sub.get('name'), 'status': 'error',
                         'changed': False, 'error': '未配置 TMDB ID'})
            continue
        errors = []; fallback_reason = ''
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
                tmdb_info = _eng()._tmdb_series_info(tmdb_id)
                if tmdb_info is None:
                    raise RuntimeError('TMDB 对照不可用，保留上次缺集记录')
            except Exception as error:
                errors.append(str(error))
                failed += 1
        prev = state.get(sid) or {}
        have = set(latest.get('episodes') or [])
        facts_ts = time.time()
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

        new_eps = sorted(have - notified)                       # 真实新增
        # TMDB 不可用/停用时不推断新缺集，仍可确认原缺集已经入库。
        missing_eps = prev_missing - have
        if tmdb_info is not None:
            aired = tmdb_info.get('aired') or set()
            # ── 缺集只认「从未入库过的集」──
            # 若只算 aired - have，那么「曾经有、后来被删」的集会被误报成缺集
            # （下架 / 洗版删旧时最容易遇到）。取 aired - (have | notified)：
            # 已通知过的集视为「到过库」，即使当下不在也不算缺集。
            missing_eps = aired - have - notified
        newly_missing = sorted(missing_eps - prev_missing)      # 本次新出现的缺集
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
                'poster': sub.get('poster', ''),
                'new_eps': sorted(_eps_to_keys(new_eps)),
                'missing_eps': sorted(_eps_to_keys(missing_eps)),
                'newly_missing': sorted(_eps_to_keys(newly_missing)),
                'refilled': sorted(_eps_to_keys(refilled)),
            })

    # ── 发送成功才记账 ──
    # send_notify=False（bot 手动查看 / 晨报 / 保存订阅后的即时检查）本身不发送消息，
    # 等价于「已送达」，同样要记账，否则下一轮会把同一批集重复播报。
    # 发送失败时：notified_episodes / missing_episodes 全部回退到本轮开始前的值，
    # 保证下一轮算出同一批 new_eps 继续重试 —— 既不丢通知也不重复刷屏。
    sent_ok = True
    if send_notify and updates:
        lines = [_eng().tg_title('🔔', '追更订阅', f'{len(updates)} 部有变化')]
        for u in updates:
            lines.append('')
            lines.append(f"📺 <b>《{html.escape(str(u['name'] or ''))}》</b>")
            if u['refilled']:
                lines.append(f"　✅ 已补齐 <b>{_fmt_ep_ranges(_keys_to_eps(u['refilled']))}</b>")
            if u['new_eps']:
                lines.append(f"　🆕 新增入库 <b>{_fmt_ep_ranges(_keys_to_eps(u['new_eps']))}</b>")
            if u['newly_missing']:
                lines.append(f"　⚠️ 缺集 <b>{_fmt_ep_ranges(_keys_to_eps(u['newly_missing']))}</b>")
        sent_ok = _eng().notify_telegram('\n'.join(lines))
        if sent_ok:
            _save_subscription_report(updates)
        else:
            log.warning('追更订阅推送失败，本轮不记账，下次继续重试')

    if not sent_ok:
        for u in updates:
            st = staged.get(u['sid'])
            if not st:
                continue
            # 回退新增集：把本轮 new_eps 从 notified_episodes 里剔除
            kept = _keys_to_eps(st['notified_episodes']) - _keys_to_eps(u['new_eps'])
            st['notified_episodes'] = sorted(_eps_to_keys(kept))
            st['latest_ep'] = (max(st['notified_episodes'], key=_ep_key_num)
                               if st['notified_episodes'] else '')
            # 缺集基线也不推进：下一轮重新判定 newly_missing
            st['missing_episodes'] = list((state.get(u['sid']) or {}).get('missing_episodes') or [])

    state.update(staged)
    _save_sub_state(state)

    outcome = 'error' if not sent_ok or (failed and not checked) else ('partial' if failed else 'success')
    error_text = 'Telegram 推送失败' if not sent_ok else ('部分订阅读取失败，请查看逐部原因' if failed else '')
    result = {'status': outcome, 'updates': updates, 'total': len(subs), 'checked': checked, 'paused': paused,
              'failed': failed, 'rows': rows, 'error': error_text,
              'sent': bool(sent_ok) if send_notify and updates else None}
    _save_subscription_check_status(
        outcome, finished_at=time.time(), duration_ms=int((time.time()-started)*1000),
        updates=len(updates), total=len(subs), checked=checked, paused=paused,
        failed=failed, rows=rows, sent=result.get('sent'), error=error_text,
    )
    try:
        summary = [f'检查 {checked} 部订阅' + (f'（{paused} 部已暂停，未检查）' if paused else ''),
                   f'发现 {len(updates)} 部有变化', f'{failed} 部读取失败/不完整']
        if send_notify and updates:
            summary.append('Telegram 推送成功' if sent_ok else 'Telegram 推送失败，等待重试')
        logger.write('追更', '订阅检查', summary,
                     extra={'shows': rows, 'checked': checked, 'paused': paused, 'failed': failed,
                            'changed': len(updates),
                            'sent': (bool(sent_ok) if send_notify and updates else None)})
    except Exception:
        pass
    return result
