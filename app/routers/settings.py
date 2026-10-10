# -*- coding: utf-8 -*-
"""规则设置 / 服务连接"""
import os, json, logging, urllib.parse, urllib.request
from app import network
from fastapi import APIRouter, Depends, HTTPException

try:
    from app.routers.deps import auth, engine, bot, _cfg
    from app.config import (load_config, save_config, EDITABLE_KEYS,
                            SENSITIVE_KEYS, mask_config, is_masked_value,
                            get_strategy, update_strategy,
                            get_cover_strategy, update_cover_strategy)
except ImportError:
    from routers.deps import auth, engine, bot, _cfg
    from config import (load_config, save_config, EDITABLE_KEYS,
                        SENSITIVE_KEYS, mask_config, is_masked_value,
                        get_strategy, update_strategy,
                        get_cover_strategy, update_cover_strategy)

router = APIRouter()


BACKGROUND_KEYS = ('background_light_url', 'background_dark_url')


def _background_config(body=None, saved=None):
    saved = load_config() if saved is None else saved
    body = body or {}
    result = {}
    for key in BACKGROUND_KEYS:
        value = body.get(key, saved.get(key, ''))
        if not isinstance(value, str):
            raise HTTPException(400, '背景地址必须是图片直链或留空')
        value = value.strip()
        if value:
            try:
                url = urllib.parse.urlsplit(value)
                valid = (len(value) <= 2048 and url.scheme in ('http', 'https') and
                         url.hostname and url.username is None and url.password is None and
                         not any(c.isspace() or ord(c) < 32 or c in '\\"<>\x7f' for c in value))
                if not valid or (url.port is not None and not 1 <= url.port <= 65535):
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, '请填写有效的 HTTP/HTTPS 图片直链；留空使用内置背景')
        result[key] = value
    return result


@router.get('/api/appearance', dependencies=[Depends(auth)])
def api_get_appearance():
    return {'status': 'success', 'appearance': _background_config()}


@router.post('/api/appearance', dependencies=[Depends(auth)])
def api_set_appearance(body: dict = None):
    cfg = load_config()
    appearance = _background_config(body, saved=cfg)
    cfg.update(appearance)
    save_config(cfg)
    return {'status': 'success', 'appearance': appearance}


@router.get('/api/strategy', dependencies=[Depends(auth)])
def api_get_strategy():
    return {'status': 'success', 'strategy': get_strategy()}


@router.get('/api/cover-strategy', dependencies=[Depends(auth)])
def api_get_cover():
    """画质对比规则（7 维）。"""
    return {'status': 'success', 'strategy': get_cover_strategy()}


@router.post('/api/cover-strategy', dependencies=[Depends(auth)])
def api_set_cover(body: dict = None):
    body = body or {}
    try:
        result = update_cover_strategy(body.get('strategy') or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'status': 'success', 'strategy': result}


@router.post('/api/cover-compare', dependencies=[Depends(auth)])
def api_cover_compare(body: dict = None):
    """对比测试：给两个文件名，按当前规则逐维展示谁胜、为什么。a=分享版 b=本地版。"""
    body = body or {}
    a, b = str(body.get('a') or '').strip(), str(body.get('b') or '').strip()
    if not a or not b:
        raise HTTPException(400, '需要 a、b 两个文件名')
    return {'status': 'success', 'compare': engine._compare_meta(a, b)}


@router.post('/api/strategy', dependencies=[Depends(auth)])
def api_set_strategy(body: dict = None):
    body = body or {}
    kwargs = {}
    if 'decision' in body:
        d = str(body['decision'])
        if d == 'balanced':
            d = 'quality_first'
        if d not in ('quality_first', 'keep_local', 'keep_share'):
            raise HTTPException(400, 'decision 必须是 quality_first / keep_local / keep_share')
        kwargs['decision'] = d
    if 'match_strategy' in body:
        ms = str(body['match_strategy'])
        if ms not in ('title_year', 'tmdb_first'):
            raise HTTPException(400, 'match_strategy 必须是 title_year / tmdb_first')
        kwargs['match_strategy'] = ms
    if 'multi_season_protect' in body:
        mp = str(body['multi_season_protect'])
        if mp in ('1', 'true', 'on'):
            mp = 'compare'
        elif mp in ('0', 'false', 'off'):
            mp = 'off'
        if mp not in ('off', 'compare'):
            raise HTTPException(400, 'multi_season_protect 必须是 off / compare')
        kwargs['multi_season_protect'] = mp
    if 'tie_keep_local' in body:
        kwargs['tie_keep_local'] = bool(body['tie_keep_local'])
    if 'season_replace_ratio' in body:
        try:
            ratio = float(body['season_replace_ratio'])
        except (TypeError, ValueError):
            raise HTTPException(400, 'season_replace_ratio 必须是 0.5~1 之间的数字')
        if not (0.5 <= ratio <= 1.0):
            raise HTTPException(400, 'season_replace_ratio 必须在 0.5 ~ 1 之间')
        kwargs['season_replace_ratio'] = ratio
    if 'exempt_keywords' in body:
        kwargs['exempt_keywords'] = body['exempt_keywords']
    if 'special_action' in body:
        sa = str(body['special_action'])
        if sa == 'keep':
            sa = 'ignore'
        if sa not in ('compare', 'ignore', 'delete'):
            raise HTTPException(400, 'special_action 必须是 compare / ignore / delete')
        kwargs['special_action'] = sa
    result = update_strategy(**kwargs)
    engine.reload_config()
    return {'status': 'success', 'strategy': result}


@router.get('/api/exempt/scan', dependencies=[Depends(auth)])
def api_scan_exempt():
    try:
        matches = engine.scan_exempt_matches()
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success', 'matches': matches, 'keywords': engine._exempt_keywords()}


def _env_overridden_keys() -> list:
    # yml/.env 里显式设置了非空值的字段：Web 页存了也不会生效，前端应据此提示/禁用
    return [k for k, env_name in _cfg.ENV_OVERRIDE_KEYS.items() if os.environ.get(env_name)]


@router.get('/api/config', dependencies=[Depends(auth)])
def api_get_config(reveal: int = 0):
    cfg = load_config()
    env_overridden = _env_overridden_keys()
    if reveal:
        return {'status': 'success', 'config': cfg, 'masked': False, 'env_overridden': env_overridden}
    return {'status': 'success', 'config': mask_config(cfg), 'masked': True,
            'sensitive_keys': SENSITIVE_KEYS, 'env_overridden': env_overridden}


@router.post('/api/config', dependencies=[Depends(auth)])
def api_set_config(body: dict = None):
    body = body or {}
    cfg = load_config()
    appearance = _background_config(body, saved=cfg)
    try:
        proxy = network.proxy_config(body, saved=cfg)
    except ValueError as error:
        raise HTTPException(400, str(error))
    env_overridden = set(_env_overridden_keys())
    skipped_env = []
    for k in EDITABLE_KEYS:
        if k not in body: continue
        if k in env_overridden:
            # yml 已经显式指定了这个字段，Web 页提交的值落盘也没用，直接跳过并告知前端
            skipped_env.append(k)
            continue
        new_val = str(body[k]).strip()
        if k in SENSITIVE_KEYS and is_masked_value(new_val):
            continue
        cfg[k] = new_val
    cfg.update({k: proxy.get(k, '') for k in network.PROXY_KEYS})
    cfg.update(appearance)
    saved = save_config(cfg)
    engine.reload_config()
    try: bot.restart()
    except Exception as e: logging.getLogger('strimkeep').warning('Bot 重启失败: %s', e)
    return {'status': 'success', 'config': mask_config(saved), 'masked': True,
            'env_overridden': list(env_overridden), 'skipped_env': skipped_env}


@router.post('/api/config/test/emby', dependencies=[Depends(auth)])
def api_test_emby(body: dict = None):
    body = body or {}
    raw_host = (body.get('emby_host') or '').strip()
    host_overridden = bool(raw_host) and raw_host.rstrip('/') != (engine.EMBY_HOST or '').rstrip('/')
    host = (raw_host or engine.EMBY_HOST or '').rstrip('/')
    key = body.get('emby_key') or ''
    if not key or is_masked_value(key):
        if host_overridden:
            # 换了地址就不能偷用旧地址保存的 Key 去测——否则真实 Key 会被发往调用方指定的任意 host
            return {'status': 'error', 'message': '更换地址后请填写完整的 API Key，不能沿用已保存的旧 Key'}
        key = engine.EMBY_KEY or ''
    if not host or not key:
        return {'status': 'error', 'message': '请填写 Emby 地址和 API Key'}
    try:
        url = f'{host}/System/Info'
        req = urllib.request.Request(url, headers={'X-Emby-Token': key})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode('utf-8'))
        msg = f"✅ 连接成功\n服务器: {data.get('ServerName', '?')}\n版本: {data.get('Version', '?')}"
        # 设置页未保存的路径也拿来比对，方便边改边测；没传就用当前生效的配置
        pm = engine.emby_path_map(body.get('emby_local_path', engine.EMBY_PATHS.local),
                                body.get('emby_share_path', engine.EMBY_PATHS.share))
        return {'status': 'success', 'message': msg + _emby_lib_paths_hint(host, key, pm)}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 连接失败: {e}'}


def _emby_lib_paths_hint(host, key, pm):
    """列出 Emby 媒体库的文件夹路径，并标出哪些对上了本地/分享库路径配置；
    拿不到就返回空串，绝不影响连接测试结果"""
    try:
        req = urllib.request.Request(f'{host}/Library/VirtualFolders', headers={'X-Emby-Token': key})
        with urllib.request.urlopen(req, timeout=5) as r:
            folders = json.loads(r.read().decode('utf-8'))
        names = {'local': '本地库', 'share': '分享库'}
        lines, hit = [], set()
        for f in (folders if isinstance(folders, list) else []):
            for loc in (f.get('Locations') or []):
                loc_n = str(loc).replace('\\', '/').rstrip('/')
                lib = pm.lib_of(loc_n, fallback=False)
                if lib:
                    tag = f' ← 匹配{names[lib]}'
                    hit.add(lib)
                else:
                    # 媒体库选的是上级目录（一个库里同时含本地/分享）也算对上
                    inner = [k for k, root in (('local', pm.local), ('share', pm.share))
                             if root and loc_n and root.startswith(loc_n + '/')]
                    tag = (' ← 包含' + '、'.join(names[k] for k in inner)) if inner else ''
                    hit.update(inner)
                lines.append(f"  {f.get('Name') or '?'}: {loc}{tag}")
        if not lines:
            return ''
        out = '\n\nEmby 媒体库路径:\n' + '\n'.join(lines)
        for lib, root in (('local', pm.local), ('share', pm.share)):
            if lib not in hit:
                out += f"\n⚠️ 当前{names[lib]}路径 {root or '(未设置)'} 未对上任何媒体库，请从上面复制正确的路径"
        return out
    except Exception:
        return ''


@router.post('/api/config/test/tmdb', dependencies=[Depends(auth)])
def api_test_tmdb(body: dict = None):
    body = body or {}
    key = body.get('tmdb_key') or ''
    if not key or is_masked_value(key): key = engine.RUNTIME_CFG.get('tmdb_key') or ''
    if not key: return {'status': 'error', 'message': '请填写 TMDB API Key'}
    try:
        params = {'language': 'zh-CN'}; headers = {}
        if key.startswith('eyJ'): headers['Authorization'] = f'Bearer {key}'
        else: params['api_key'] = key
        url = 'https://api.themoviedb.org/3/configuration?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        with network.open_external(req, timeout=10, config=network.proxy_config(body, saved=load_config())) as r:
            data = json.loads(r.read().decode('utf-8'))
        img = (data.get('images') or {}).get('secure_base_url', '?')
        return {'status': 'success', 'message': f'✅ TMDB Key 有效\n图片 CDN: {img}'}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 验证失败: {e}'}


@router.post('/api/config/test/telegram', dependencies=[Depends(auth)])
def api_test_telegram(body: dict = None):
    body = body or {}
    token = (body.get('telegram_bot_token') or '').strip()
    chat_id = (body.get('telegram_chat_id') or '').strip()
    if not token or is_masked_value(token): token = engine.RUNTIME_CFG.get('telegram_bot_token') or ''
    if not chat_id: chat_id = engine.RUNTIME_CFG.get('telegram_chat_id') or ''
    if not token or not chat_id:
        return {'status': 'error', 'message': '请填写 Bot Token 和 Chat ID'}
    try:
        text = '✅ <b>StrimKeep</b> 测试消息\n如果你看到这条消息，说明 Telegram 通知已配置成功。'
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with network.open_external(req, timeout=10, config=network.proxy_config(body, saved=load_config())) as r:
            resp = json.loads(r.read().decode('utf-8'))
        if resp.get('ok'): return {'status': 'success', 'message': '✅ 已发送测试消息，请查看 Telegram'}
        return {'status': 'error', 'message': f"❌ 发送失败: {resp.get('description', '未知错误')}"}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 发送失败: {e}'}


@router.post('/api/config/test/proxy', dependencies=[Depends(auth)])
def api_test_proxy(body: dict = None):
    try:
        cfg = network.proxy_config(body, saved=load_config())
        if cfg['http_proxy_enabled'] != '1':
            return {'status': 'error', 'message': '请先开启 HTTP 代理，再测试连接'}
        request = urllib.request.Request('https://api.themoviedb.org/3/configuration')
        try:
            with network.open_external(request, timeout=10, config=cfg) as response:
                response.read(1024)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            error.close()
        return {'status': 'success', 'message': '已通过 HTTP 代理连接 TMDB，HTTPS 连接正常；可继续测试 TMDB 密钥'}
    except Exception as error:
        return {'status': 'error', 'message': str(error)}
