"""Version grouping and lexicographic quality rules. Unknown never equals low quality."""
import math
import re
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .media import NeedsReview, component, release_group, technical_name
from .audio import known_representative


KEYS = ('group', 'source', 'resolution', 'dynamic_range', 'fps', 'bit_depth',
        'audio', 'codec', 'size')
LABELS = dict(zip(KEYS, ('发布组', '资源类型与平台', '分辨率', '动态范围', '帧率',
                         '色深', '音频规格', '视频编码', '文件大小')))
LABELS['revision'] = '修订版本'  # Decode existing task snapshots only.
CHOICES = {
    'source': ['uhd-remux', 'bluray-remux', 'uhd-bluray', 'bluray', 'web-dl', 'webrip', 'hdtv'],
    'dynamic_range': ['dovi', 'dovi-p7', 'dovi-p8', 'dovi-p5', 'hdr10plus', 'hdrvivid', 'hdr10', 'hlg', 'sdr'],
    'codec': ['av1', 'hevc', 'h264', 'vp9', 'mpeg2video'],
    'audio': ['truehd', 'dts_hd_ma', 'dts_hd_hra', 'dts', 'flac', 'pcm', 'av3a', 'eac3', 'ac3', 'aac', 'opus', 'mp3'],
}
PLATFORMS = {'atvp': 'Apple TV+', 'hulu': 'Hulu', 'itunes': 'iTunes', 'hmax': 'HBO Max',
             'dsnp': 'Disney+', 'nf': 'Netflix', 'amzn': 'Amazon', 'pmtp': 'Paramount+',
             'cr': 'Crunchyroll', 'tving': 'TVING', 'friday': 'friDay', 'kktv': 'KKTV',
             'myvideo': 'MyVideo', 'hami': 'Hami', 'catchplay': 'CATCHPLAY', 'viu': 'Viu',
             'now': 'Now Player', 'baha': '巴哈姆特', 'abema': 'Abema', 'linetv': 'LINE TV',
             'mytvsuper': 'myTV SUPER', 'wetv': '腾讯 / WeTV', 'iq': '爱奇艺', 'youku': '优酷', 'bili': '哔哩哔哩'}
PLATFORM_ALIASES = {'atvp': ('ATVP', 'AppleTV'), 'hulu': ('HULU',), 'itunes': ('iTunes',),
                    'hmax': ('HMAX', 'HBO', 'MAX'), 'dsnp': ('DSNP', 'DisneyPlus'), 'nf': ('NF', 'Netflix'),
                    'amzn': ('AMZN', 'Amazon'), 'pmtp': ('PMTP', 'ParamountPlus'), 'cr': ('CR', 'Crunchyroll'),
                    'tving': ('TVING',), 'friday': ('friDay',), 'kktv': ('KKTV',), 'myvideo': ('MyVideo',),
                    'hami': ('Hami',), 'catchplay': ('CATCHPLAY',), 'viu': ('Viu',), 'now': ('NowPlayer',),
                    'baha': ('Baha',), 'abema': ('Abema',), 'linetv': ('LINETV',),
                    'mytvsuper': ('MyTVSuper',), 'wetv': ('WeTV', 'Tencent'), 'iq': ('IQ', 'IQIYI'),
                    'youku': ('YOUKU',), 'bili': ('Bili', 'Bilibili')}
COMMON_FPS = (24, 25, 30, 48, 50, 60, 72, 90, 100, 120, 144, 165, 240)
# UI catalogues are suggestions, never automatically enabled deletion rules.
OPTIONS = {
    'source': {**{v: v.upper() for v in CHOICES['source']},
               **{f'web-dl:{k}': f'WEB-DL · {v}' for k, v in PLATFORMS.items()},
               'web-dl:other': 'WEB-DL · 未标注 / 未识别平台', 'dvd': 'DVD / DVD Rip'},
    'resolution': {'4320': '4320p / 8K', '2160': '2160p / 4K', '1440': '1440p / 2K',
                   '1080': '1080p', '720': '720p', 'sd': 'SD（576p 及以下）'},
    'dynamic_range': {v: v for v in CHOICES['dynamic_range']},
    'fps': {str(v): f'{v} fps' for v in reversed(COMMON_FPS)},
    'bit_depth': {str(v): f'{v} bit' for v in (16, 12, 10, 8)},
    'audio': {v: v for v in CHOICES['audio']},
    'codec': {v: v for v in CHOICES['codec']},
}
OPTIONS['source']['web-dl'] = 'WEB-DL · 所有平台（不细分）'
OPTIONS['source'].update({'uhd-remux': 'UHD Remux', 'bluray-remux': 'Blu-ray Remux',
                          'uhd-bluray': 'UHD Blu-ray', 'bluray': 'Blu-ray', 'webrip': 'WEBRip'})
OPTIONS['dynamic_range'].update({'dovi': 'Dolby Vision（未列出 / 未识别 Profile）',
    'dovi-p7': 'Dolby Vision Profile 7', 'dovi-p5': 'Dolby Vision Profile 5',
    'dovi-p8': 'Dolby Vision Profile 8', 'hdr10plus': 'HDR10+', 'hdrvivid': 'HDR Vivid', 'hdr10': 'HDR10', 'hlg': 'HDR / HLG', 'sdr': 'SDR'})
OPTIONS['audio'].update({'truehd': 'TrueHD', 'dts_hd_ma': 'DTS-HD MA', 'dts_hd_hra': 'DTS-HD HRA',
    'dts': 'DTS', 'flac': 'FLAC', 'pcm': 'LPCM', 'eac3': 'DD+ / E-AC-3', 'ac3': 'DD / AC-3',
    'aac': 'AAC', 'av3a': 'Audio Vivid / AV3A', 'opus': 'Opus', 'mp3': 'MP3'})
OPTIONS['codec'].update({'av1': 'AV1', 'hevc': 'H.265 / HEVC', 'h264': 'H.264 / AVC', 'vp9': 'VP9', 'mpeg2video': 'MPEG-2'})
SUGGESTED_ORDER = {
    'group': [],
    'source': ['uhd-remux', 'bluray-remux', 'uhd-bluray', 'bluray',
               *[f'web-dl:{p}' for p in PLATFORMS], 'web-dl:other', 'webrip', 'hdtv', 'dvd'],
    'resolution': list(OPTIONS['resolution']),
    'dynamic_range': ['dovi-p7', 'dovi-p5', 'dovi-p8', 'dovi', 'hdr10plus', 'hdrvivid', 'hdr10', 'hlg', 'sdr'],
    'fps': list(OPTIONS['fps']), 'bit_depth': list(OPTIONS['bit_depth']),
    'audio': ['truehd', 'dts_hd_ma', 'dts_hd_hra', 'pcm', 'flac', 'av3a', 'eac3', 'dts', 'ac3', 'aac', 'opus', 'mp3'],
    'codec': ['av1', 'hevc', 'h264', 'vp9', 'mpeg2video'],
}


class Rule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: Literal['group', 'source', 'resolution', 'dynamic_range', 'fps', 'bit_depth', 'audio', 'revision', 'codec', 'size']
    enabled: bool = False
    preferences: list[str] = Field(default_factory=list, max_length=50)
    platforms: list[str] = Field(default_factory=list, max_length=30)
    descending: bool = True
    order: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def check_preferences(self):
        for field in ('preferences', 'platforms'):
            values = [v.strip().casefold() for v in getattr(self, field)]
            if any(not v or len(v) > 40 or any(c in v for c in '\r\n\x00') for v in values) or len(set(values)) != len(values):
                raise ValueError('偏好必须是非空、无重复的短名称。')
            setattr(self, field, values)
        if self.key in CHOICES and any(v not in CHOICES[self.key] for v in self.preferences):
            raise ValueError('请选择本规则支持的偏好名称。')
        self.order = [v.strip().casefold() for v in self.order]
        if (len(set(self.order)) != len(self.order)
                or any(not v or len(v) > 40 or any(c in v for c in '\r\n\x00') for v in self.order)):
            raise ValueError('排序选项必须非空且不能重复。')
        if self.order and (self.preferences or self.platforms):
            raise ValueError('新版排序与旧版偏好不能同时使用。')
        if self.order and self.key in ('revision', 'size'):
            raise ValueError('修订版本和大小使用数值方向，不使用选项排序。')
        if self.key != 'group' and any(v not in OPTIONS.get(self.key, {}) for v in self.order):
            raise ValueError('排序中含本规则不支持的选项。')
        if self.key == 'source' and 'web-dl' in self.order and any(v.startswith('web-dl:') for v in self.order):
            raise ValueError('WEB-DL 所有平台不能与平台细分同时使用。')
        if self.enabled and self.key in ('group', 'source', 'dynamic_range', 'audio', 'codec') and not (self.preferences or self.order):
            raise ValueError('启用文字偏好规则时须明确配置优先顺序。')
        return self


class UpgradePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    coexist_version: int = Field(default=0, ge=0, le=1)
    mode: Literal['pending', 'skip', 'upgrade'] = 'pending'
    deduplicate_identical: bool = True
    # 以下四项是旧配置的兼容字段：读入后一律归零，不再影响分组。
    resolution_groups: bool = False
    dynamic_groups: Literal['off', 'three', 'dovi'] = 'off'
    fps_groups: bool = False
    unknown_fps_low: bool = False
    season_layout: Literal['standard', 'spec'] = 'standard'
    rules: list[Rule] = Field(default_factory=lambda: [Rule(key=k) for k in KEYS], max_length=10)

    @model_validator(mode='after')
    def check_rules(self):
        keys = [r.key for r in self.rules]
        if len(keys) != len(set(keys)) or set(keys) not in (set(KEYS), set(KEYS) | {'revision'}):
            raise ValueError('九项比较规则须各出现一次，可独立关闭和排序。')
        if self.mode == 'upgrade' and not any(r.enabled for r in self.rules):
            raise ValueError('允许洗版前至少启用一项比较规则。')
        # 共存维度只剩「杜比 / 非杜比」：分辨率、帧率、三档动态范围、规格季目录已移除，旧值静默收敛。
        self.resolution_groups = False
        self.fps_groups = False
        self.unknown_fps_low = False
        if self.dynamic_groups == 'three':
            self.dynamic_groups = 'off'
        self.season_layout = 'standard'
        return self


def public_policy(value=None):
    """Simplify future tasks without rewriting snapshots of in-flight jobs."""
    raw = dict(value or {})
    policy = UpgradePolicy.model_validate(raw).model_dump()
    policy['deduplicate_identical'] = True
    removed_active = (raw.get('resolution_groups') or raw.get('fps_groups') or raw.get('unknown_fps_low')
                      or raw.get('dynamic_groups') == 'three' or raw.get('season_layout', 'standard') != 'standard'
                      or any(r['enabled'] and r['key'] == 'revision' for r in policy['rules']))
    policy['rules'] = [r for r in policy['rules'] if r['key'] != 'revision']
    policy.update(resolution_groups=False, fps_groups=False, unknown_fps_low=False,
                  dynamic_groups='dovi' if raw.get('dynamic_groups') == 'dovi' else 'off',
                  season_layout='standard')
    if policy['mode'] == 'upgrade' and (removed_active or not any(r['enabled'] for r in policy['rules'])):
        policy['mode'] = 'pending'
    return policy


def positive(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def resolution(media):
    width, height = positive(media.get('width')), positive(media.get('height'))
    if width is None or height is None:
        return None
    # 3840x1600 and 1920x800 retain their UHD/HD class after cinematic cropping.
    return 4320 if max(width, height) >= 7000 or min(width, height) >= 4000 else 2160 if max(width, height) >= 3500 or min(width, height) >= 2000 else 1440 if max(width, height) >= 2500 or min(width, height) >= 1400 else 1080 if max(width, height) >= 1800 or min(width, height) >= 1000 else 720 if max(width, height) >= 1200 or min(width, height) >= 700 else int(min(width, height))


def frame_rate(media):
    try:
        value = float(Fraction(str(media.get('fps_raw') or media.get('fps') or '0')))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if not positive(value):
        return None
    for actual, rounded in ((24000/1001, 24), (30000/1001, 30), (60000/1001, 60), (120000/1001, 120)):
        if abs(value - actual) < .005:
            return rounded
    return round(value, 3)


def dynamic(media):
    return {'Dolby Vision': 'dovi', 'Dolby Vision HDR': 'dovi', 'HDR10+': 'hdr10plus',
            'HDR Vivid': 'hdrvivid', 'HDR10': 'hdr10', 'HDR PQ': 'hdr10', 'HLG': 'hlg', 'SDR': 'sdr'}.get(media.get('dynamic_range'))


def facts(original_name, media, size):
    stem = Path(original_name).stem
    # 与命名共用同一个判定：WEB-DL 的 -DL 不是发布组，否则洗版会拿假组名比对。
    group = release_group(original_name)
    revision = re.search(r'(?i)(?<![a-z0-9])V([1-9]\d?)(?![a-z0-9])', stem)
    source = None
    uhd = bool(re.search(r'(?i)\bUHD\b', stem))
    if re.search(r'(?i)\b(?:BDREMUX|REMUX)\b', stem):
        # REMUX alone does not establish UHD vs Blu-ray provenance.
        source = 'uhd-remux' if uhd else 'bluray-remux' if re.search(r'(?i)\b(?:BLU[ ._-]?RAY|BDREMUX)\b', stem) else None
    elif re.search(r'(?i)\bBLU[ ._-]?RAY\b', stem):
        source = 'uhd-bluray' if uhd else 'bluray'
    elif re.search(r'(?i)\bWEB[ ._-]?DL\b', stem):
        source = 'web-dl'
    elif re.search(r'(?i)\bWEBRIP\b', stem):
        source = 'webrip'
    elif re.search(r'(?i)\bHDTV\b', stem):
        source = 'hdtv'
    elif re.search(r'(?i)\bDVD(?:RIP)?\b', stem):
        source = 'dvd'
    platforms = [key for key, aliases in PLATFORM_ALIASES.items()
                 if any(re.search(r'(?i)(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])', stem) for alias in aliases)]
    return {'group': group.casefold() if group else None, 'source': source,
            'platform': platforms[0] if len(platforms) == 1 else None, 'platform_ambiguous': len(platforms) > 1,
            'resolution': resolution(media),
            'dynamic_range': dynamic(media), 'dv_profile': media.get('dolby_vision_profile'),
            'fps': frame_rate(media), 'bit_depth': positive(media.get('bit_depth')),
            'audio': media.get('audio', []), 'revision': int(revision[1]) if revision else None,
            'codec': (media.get('codec') or '').casefold() or None, 'size': positive(size)}


def group_key(data, policy):
    if policy['dynamic_groups'] == 'off':
        return 'default'
    value = data['dynamic_range']
    if value is None:
        raise NeedsReview('共存分组缺少可靠动态范围。')
    return 'DoVi' if value == 'dovi' else 'NonDoVi'

def rank(value, preferences, label):
    if value is None or value not in preferences:
        raise NeedsReview(f'{label}缺失或未列入偏好，无法可靠比较。')
    return -preferences.index(value)


def rule_value(data, rule):
    key = rule['key']
    order = rule.get('order', [])
    def priority(value):
        return rank(value, order or rule['preferences'], LABELS[key])
    if key in ('group', 'codec'):
        return priority(data[key]), data[key]
    if key == 'source':
        if order:
            value = data['source']
            flat = order
            if value == 'web-dl' and value not in flat:
                if data.get('platform_ambiguous'):
                    raise NeedsReview('原名含多个平台标记，无法可靠比较。')
                value += ':' + (data['platform'] or 'other')
            return priority(value), {'type': data['source'], 'platform': data['platform']}
        value = [rank(data['source'], rule['preferences'], '资源类型')]
        if rule['platforms']:
            value.append(rank(data['platform'], rule['platforms'], '平台'))
        return tuple(value), {'type': data['source'], 'platform': data['platform']}
    if key == 'dynamic_range':
        value = data[key]
        if value == 'dovi':
            specific = f'dovi-p{data["dv_profile"]}'
            flat = order or rule['preferences']
            value = specific if specific in flat else 'dovi'
        return priority(value), value
    if key == 'audio':
        chosen = known_representative(data['audio'], order or rule['preferences'])
        if chosen is None:
            raise NeedsReview('缺少可比较音轨。')
        score, audio = chosen
        return score, {k: audio.get(k) for k in ('codec', 'channels', 'language')}
    value = data[key]
    if value is None:
        raise NeedsReview(f'{LABELS[key]}未知，不能跳过本规则继续淘汰。')
    if order:
        token = 'sd' if key == 'resolution' and value <= 576 else str(int(value)) if float(value).is_integer() else str(value)
        return priority(token), value
    return (value if rule['descending'] else -value), value


def compare(new, old, policy, skip_unknown=False):
    evidence = []
    if not any(rule['enabled'] for rule in policy['rules']):
        raise NeedsReview('未启用任何洗版比较规则，不能自动淘汰。')
    for rule in policy['rules']:
        if not rule['enabled']:
            continue
        try:
            left, left_label = rule_value(new, rule)
            right, right_label = rule_value(old, rule)
        except NeedsReview as exc:
            if not skip_unknown:
                raise
            evidence.append({'rule': rule['key'], 'label': LABELS[rule['key']],
                             'result': 'unknown', 'reason': str(exc)})
            continue
        result = (left > right) - (left < right)
        evidence.append({'rule': rule['key'], 'label': LABELS[rule['key']], 'new': left_label,
                         'old': right_label, 'result': result})
        if result:
            return result, evidence
    return 0, evidence


def version_layout(plan, original_name, group, policy):
    if group == 'default':
        return
    if plan['hint']['media_type'] == 'tv':
        if policy['season_layout'] == 'spec':
            plan['parts'][-1] += ' - ' + group
        hint, details = plan['hint'], plan['details']
        code = f'S{hint["season"]:02d}E{hint["episode"]:02d}'
        if hint['end_episode'] != hint['episode']:
            code += f'-E{hint["end_episode"]:02d}'
        title = component(details.get('name') or details.get('title'), 100)
        year = (details.get('first_air_date') or details.get('release_date'))[:4]
        stem = f'{title}.{year} - {code} - {group}.{technical_name(plan["media"], original_name, policy)}'
    else:
        stem = plan['parts'][-1] + ' - ' + group + '.' + technical_name(plan['media'], original_name, policy)
    if len(stem.encode()) > 220:
        raise NeedsReview('共存版本名称超长，保留原名转待处理。')
    plan['target_name'] = component(stem, 220) + Path(original_name).suffix
