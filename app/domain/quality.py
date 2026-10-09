# -*- coding: utf-8 -*-
"""
domain.quality —— 画质对比领域模型（纯函数）

从 core.py 原样迁出，行为零变化。
约束：不做任何 IO，不读取环境变量，不访问文件系统 / SQLite / Emby / TMDB；
给定相同输入必然返回相同输出。

对外语义：compare_cover(local_name, share_name, cover) → 1 / -1 / 0
（1=a 更优，-1=b 更优，0=全部打平，由调用方按「平局保留本地/分享」处理）。
"""
import re

RE_DV = re.compile(r'(?<![A-Z0-9])(?:DV|DOVI)(?![A-Z0-9])|DOLBY[ ._-]?VISION')

# ═══════════════════ 画质对比规则（7 维，双库治理唯一的版本比较依据） ═══════════════════
# 对齐上游 TgtoDrive「影视整理」自定义规则。按规则顺序逐维比较：
#   两个版本在该维度的档位序号不同 → 序号小（靠前）者胜；相同或都未识别 → 进入下一维。
# 没有任何隐藏打分：能影响结果的只有「规则顺序 / 启用开关 / 档位顺序 / 发布组列表」。
# 全部规则打平 = 平局，由「平局保留本地/分享」开关决定，这里不兜底。
COVER_RULE_ORDER = ['release_group', 'source', 'resolution', 'dolby',
                    'bitdepth', 'audio', 'fps']

COVER_RULE_META = {
    'release_group': {'label': '发布组优先级',
                      'desc': '按你维护的发布组列表比较；越靠前优先级越高，不在列表内的排最后。'},
    'source': {'label': '资源类型优先级',
               'desc': '识别 UHD/蓝光/WEB 等片源类型；其他或未识别格式默认排在最后。'},
    'resolution': {'label': '分辨率优先级',
                   'desc': '按画面高度比较；2K/1440p 作为兼容档位保留，未识别分辨率排在最后。'},
    'dolby': {'label': '动态范围优先级',
              'desc': 'Dolby Vision 会细分 P7、P5、P8，并识别 HDR Vivid；未识别格式排在最后。'},
    'bitdepth': {'label': '色深优先级',
                 'desc': '识别文件名中的 bit depth；未识别色深排在最后。'},
    'audio': {'label': '音频规格优先级',
              'desc': '按文件名中识别到的最高音频规格比较；未识别格式排在最后。'},
    'fps': {'label': '帧率优先级',
            'desc': '59.94 / 29.97 / 23.976 会分别归入 60 / 30 / 24 档，未识别帧率排在最后。'},
}

COVER_TIERS_DEFAULT = {
    'source': ['UHD Remux', 'Remux', 'UHD BluRay / BDMV', 'BluRay', 'WEB-DL', 'WEBRip', 'HDTV', 'DVD', '其他 / 未识别'],
    'resolution': ['4320p / 8K', '2160p / 4K', '1440p / 2K', '1080p', '720p', 'SD（576p 及以下）', '其他 / 未识别'],
    'dolby': ['Dolby Vision Profile 7', 'Dolby Vision Profile 5', 'Dolby Vision Profile 8',
              'Dolby Vision（未识别 Profile）', 'HDR10+', 'HDR Vivid', 'HDR10', 'HDR / HLG', 'SDR', '其他 / 未识别'],
    'bitdepth': ['12bit', '10bit', '8bit', '其他 / 未识别'],
    'audio': ['TrueHD Atmos / DTS:X', 'TrueHD / DTS-HD MA', 'LPCM / FLAC', 'Audio Vivid / AV3A',
              'DD+ Atmos', 'DD+ / E-AC-3', 'DTS', 'AC3 / DD', 'AAC', '其他 / 未识别'],
    'fps': ['60 / 59.94 fps', '50 fps', '30 / 29.97 fps', '25 fps', '24 / 23.976 fps', '其他 / 未识别'],
    'release_group': [],
}

COVER_ENABLED_DEFAULT = {
    'release_group': False, 'source': True, 'resolution': True, 'dolby': True,
    'bitdepth': True, 'audio': True, 'fps': True,
}


def _cover_rule(key, enabled=None, tiers=None, groups=None):
    return {'key': key, 'label': COVER_RULE_META[key]['label'], 'desc': COVER_RULE_META[key]['desc'],
            'enabled': COVER_ENABLED_DEFAULT.get(key, True) if enabled is None else bool(enabled),
            'tiers': list(COVER_TIERS_DEFAULT.get(key, []) if tiers is None else tiers),
            'groups': list(groups or [])}


def cover_default_strategy():
    """默认画质对比规则：顺序/开关/档位全部对齐上游 TgtoDrive。"""
    return {'rules': [_cover_rule(k) for k in COVER_RULE_ORDER]}


def normalize_cover(data):
    """把任意（可能残缺/过期/来自旧版本）的配置规范成完整 7 条规则。

    - 未知 key 丢弃，缺失的规则按默认补在末尾，重复 key 只取第一条；
    - 档位只允许「重排」：用户顺序里不存在于默认档位的项丢弃，默认里缺的项补在末尾
      （「其他 / 未识别」始终保持最后一档，避免用户误把未识别排到最前）；
    - 发布组列表去空去重；旧版 allow_wash 字段忽略。
    """
    raw = data.get('rules') if isinstance(data, dict) else None
    seen, rules = set(), []
    for r in (raw if isinstance(raw, list) else []):
        if not isinstance(r, dict):
            continue
        key = r.get('key')
        if key not in COVER_RULE_META or key in seen:
            continue
        seen.add(key)
        default_tiers = COVER_TIERS_DEFAULT.get(key, [])
        tiers = []
        for t in (r.get('tiers') or []):
            t = str(t)
            if t in default_tiers and t not in tiers:
                tiers.append(t)
        tiers += [t for t in default_tiers if t not in tiers]
        if '其他 / 未识别' in tiers:
            tiers.remove('其他 / 未识别')
            tiers.append('其他 / 未识别')
        groups = []
        for g in (r.get('groups') or []):
            g = str(g).strip()
            if g and g not in groups:
                groups.append(g)
        rules.append(_cover_rule(key, r.get('enabled', COVER_ENABLED_DEFAULT.get(key, True)), tiers, groups))
    for k in COVER_RULE_ORDER:
        if k not in seen:
            rules.append(_cover_rule(k))
    return {'rules': rules}


def _dim_source(s):
    su = s.upper().replace(' ', '')
    is_uhd = '2160' in su or 'UHD' in su
    if 'REMUX' in su:
        return 'UHD Remux' if is_uhd else 'Remux'
    if 'BLURAY' in su or 'BLU-RAY' in su or 'BDMV' in su:
        return 'UHD BluRay / BDMV' if is_uhd else 'BluRay'
    if 'WEB-DL' in su or 'WEBDL' in su:
        return 'WEB-DL'
    if 'WEBRIP' in su:
        return 'WEBRip'
    if 'HDTV' in su:
        return 'HDTV'
    if 'DVD' in su:
        return 'DVD'
    return '其他 / 未识别'


def _dim_resolution(s):
    su = s.upper()
    if '4320' in su or re.search(r'(?<![A-Z0-9])8K(?![A-Z0-9])', su): return '4320p / 8K'
    if '2160' in su or re.search(r'(?<![A-Z0-9])4K(?![A-Z0-9])', su): return '2160p / 4K'
    if '1440' in su or re.search(r'(?<![A-Z0-9])2K(?![A-Z0-9])', su): return '1440p / 2K'
    if '1080' in su: return '1080p'
    if '720' in su: return '720p'
    if '576' in su or '480' in su: return 'SD（576p 及以下）'
    return '其他 / 未识别'


def _dim_dolby(s):
    su = s.upper()
    if RE_DV.search(su):
        m = re.search(r'(?:DOVI|DOLBY[ ._-]?VISION|DV)[ ._-]*P([578])', su)
        if m:
            return f'Dolby Vision Profile {m.group(1)}'
        return 'Dolby Vision（未识别 Profile）'
    if 'HDR10+' in su or 'HDR10PLUS' in su: return 'HDR10+'
    if 'HDRVIVID' in su.replace(' ', '').replace('.', '') : return 'HDR Vivid'
    if 'HDR10' in su: return 'HDR10'
    if 'HDR' in su or 'HLG' in su: return 'HDR / HLG'
    if re.search(r'(?<![A-Z0-9])SDR(?![A-Z0-9])', su): return 'SDR'
    return '其他 / 未识别'


def _dim_bitdepth(s):
    su = s.upper()
    if re.search(r'12[ ._-]?BIT', su): return '12bit'
    if re.search(r'10[ ._-]?BIT', su): return '10bit'
    if re.search(r'(?<![0-9])8[ ._-]?BIT', su): return '8bit'
    return '其他 / 未识别'


def _dim_audio(s):
    su = s.upper().replace('.', ' ')
    if 'TRUEHD' in su and 'ATMOS' in su: return 'TrueHD Atmos / DTS:X'
    if 'DTS:X' in su or 'DTS-X' in su: return 'TrueHD Atmos / DTS:X'
    if 'TRUEHD' in su or 'DTS-HD' in su: return 'TrueHD / DTS-HD MA'
    if 'LPCM' in su or 'FLAC' in su or 'PCM' in su: return 'LPCM / FLAC'
    if 'AUDIO VIVID' in su or 'AV3A' in su: return 'Audio Vivid / AV3A'
    if 'DDP' in su or 'DD+' in su:
        return 'DD+ Atmos' if 'ATMOS' in su else 'DD+ / E-AC-3'
    if 'E-AC-3' in su or 'EAC3' in su: return 'DD+ / E-AC-3'
    if 'DTS' in su: return 'DTS'
    if 'AC3' in su or 'DD5' in su or 'DOLBY DIGITAL' in su: return 'AC3 / DD'
    if 'AAC' in su: return 'AAC'
    return '其他 / 未识别'


def _dim_fps(s):
    su = s.upper().replace(' ', '')
    if '59.94' in su or '60FPS' in su or '120FPS' in su: return '60 / 59.94 fps'
    if '50FPS' in su: return '50 fps'
    if '29.97' in su or '30FPS' in su: return '30 / 29.97 fps'
    if '25FPS' in su: return '25 fps'
    if '23.976' in su or '24FPS' in su: return '24 / 23.976 fps'
    return '其他 / 未识别'


def _dim_release_group(name):
    stem = re.sub(r'(?i)\.(strm|mkv|mp4|ts|m2ts|avi|iso)$', '', name)
    m = re.search(r'-([A-Za-z0-9@#&~^]{2,24})$', stem)
    return m.group(1) if m else ''


def recognize_dims(name):
    """从文件名识别各维度档位标签（所有比较的唯一输入）。"""
    return {
        'source': _dim_source(name),
        'resolution': _dim_resolution(name),
        'dolby': _dim_dolby(name),
        'bitdepth': _dim_bitdepth(name),
        'audio': _dim_audio(name),
        'fps': _dim_fps(name),
        'release_group': _dim_release_group(name),
    }


def _tier_rank(label, tiers):
    """label 在 tiers 里的序号；找不到（未识别/用户改了档位名）→ 最后一档。"""
    try:
        return tiers.index(label)
    except ValueError:
        return max(0, len(tiers) - 1)


def _dim_rank(key, rule, dims):
    """某版本在某维度的序号（越小越优）。返回 (序号, 展示文本)。"""
    if key == 'release_group':
        groups = rule.get('groups') or []
        g = dims.get('release_group') or ''
        return (groups.index(g) if g in groups else len(groups)), (g or '未识别')
    tiers = rule.get('tiers') or COVER_TIERS_DEFAULT.get(key) or []
    label = dims.get(key) or '其他 / 未识别'
    return _tier_rank(label, tiers), label


def _active_rules(cover):
    """实际参与比较的规则：已启用、有可比内容。"""
    out = []
    for r in (cover.get('rules') or []):
        key = r.get('key')
        if r.get('enabled') is False:
            continue
        if key == 'release_group':
            if not (r.get('groups') or []):
                continue          # 没维护发布组列表 = 该维度无意义，跳过
        elif not (r.get('tiers') or COVER_TIERS_DEFAULT.get(key)):
            continue
        out.append(r)
    return out


def compare_cover(a_name, b_name, cover):
    """按画质对比规则比较两个版本：1（a 更优）/ -1（b 更优）/ 0（全部打平）。"""
    da, db = recognize_dims(a_name), recognize_dims(b_name)
    for r in _active_rules(cover):
        ia, _ = _dim_rank(r['key'], r, da)
        ib, _ = _dim_rank(r['key'], r, db)
        if ia != ib:
            return 1 if ia < ib else -1
    return 0


def explain_compare(a_name, b_name, cover):
    """逐维解释一次比较（给 Web 展示 / 审计留痕用）。

    返回 {'result': 1|-1|0, 'decided_by': 规则key|None,
          'dims': [{key,label,a,b,verdict:'a'|'b'|'tie'|'skip', reason}...]}
    verdict=skip 表示该维度未参与（关闭 / 发布组未维护列表）；
    决出胜负之后的维度标 'after'（不再比较）。
    """
    da, db = recognize_dims(a_name), recognize_dims(b_name)
    dims, decided, result = [], None, 0
    active = {id(r) for r in _active_rules(cover)}
    for r in (cover.get('rules') or []):
        key = r.get('key')
        meta = COVER_RULE_META.get(key, {})
        row = {'key': key, 'label': r.get('label') or meta.get('label') or key}
        ia, ta = _dim_rank(key, r, da)
        ib, tb = _dim_rank(key, r, db)
        row['a'], row['b'] = ta, tb
        if id(r) not in active:
            row['verdict'] = 'skip'
            row['reason'] = '已关闭' if r.get('enabled') is False else '未维护发布组列表'
        elif decided is not None:
            row['verdict'] = 'after'
            row['reason'] = '上一维已分胜负'
        elif ia != ib:
            decided, result = key, (1 if ia < ib else -1)
            row['verdict'] = 'a' if ia < ib else 'b'
            row['reason'] = '决胜维度'
        else:
            row['verdict'] = 'tie'
            row['reason'] = '打平，继续比下一维'
        dims.append(row)
    return {'result': result, 'decided_by': decided, 'dims': dims}


def quality_label(name):
    """人类可读的版本标签（展示/审计用，不参与任何比较）。"""
    d = recognize_dims(name)
    parts = [d[k] for k in ('resolution', 'source', 'dolby') if d[k] != '其他 / 未识别']
    return ' · '.join(parts) if parts else '未识别规格'
