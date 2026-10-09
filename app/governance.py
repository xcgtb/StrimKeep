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
                       quality_label, RE_SXXEXX, compare_cover, explain_compare)
except ImportError:
    from core import (esc, parse_season_dir, get_ep, title_key,
                      governance_title_key, analyze_season_episodes, parse_emby_library,
                      quality_label, RE_SXXEXX, compare_cover, explain_compare)

# 纯决策规则已迁至 domain/governance.py；本文件保留同签名包装（负责从全局策略取参数后注入）。
try:
    from .domain import governance as _dom
    from . import plan_guard as _plan_guard
    from . import tasks as _tasks
    from .lib import LibraryScanError
except ImportError:
    from domain import governance as _dom
    import plan_guard as _plan_guard
    import tasks as _tasks
    from lib import LibraryScanError


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


def _strategy():
    return _cfg.get_strategy()


def _cover_strategy():
    """画质对比规则（config 内部按文件签名缓存，保存后立即生效）。"""
    return _cfg.get_cover_strategy()


def _cmp_versions(a_name, b_name):
    """版本比较唯一入口（治理/去重共用）：只按 7 维画质对比规则，1(a优)/-1(b优)/0(平)。
    没有任何隐藏打分或兜底；平局由调用方按「平局保留本地/分享」决定。"""
    return compare_cover(a_name, b_name, _cover_strategy())


def _tie_share_wins():
    """平局时分享是否胜出（由 Web「平局保留本地」开关决定）。"""
    return not _eng()._strategy()['tie_keep_local']


def _replace_ratio():
    """剧集「分享达标率」阈值：Web 可调（0.5~1.0），缺失/非法回落 engine.SEASON_REPLACE_RATIO。"""
    default = _eng().SEASON_REPLACE_RATIO
    try:
        v = float(_eng()._strategy().get('season_replace_ratio', default))
    except (TypeError, ValueError):
        return default
    return v if 0.5 <= v <= 1.0 else default

def _exempt_keywords():
    return _eng()._strategy()['exempt_keywords']

def _ep(p: Path):
    parent = p.parent
    allow = parse_season_dir(parent.name) is not None
    if not allow:
        # 从当前路径向上找本工具的媒体根；只有位于含“剧”的分类目录下才允许
        # 裸 EP/Episode。找不到根时宁可保守返回 None。
        for root in (_eng().L_ROOT, _eng().S_ROOT):
            try:
                parent.relative_to(root)
                allow = _eng()._under_tv_category(parent, root)
                break
            except (ValueError, OSError):
                continue
    return get_ep(p.name, parent.name, allow_bare_ep=allow)

def _filter_evidence_by_season(files, season):
    """治理证据只展示该动作所在季的文件（修复跨季展示 bug）。"""
    if season is None:
        return list(files)
    out = []
    for f in files:
        p = Path(f)
        dir_s = parse_season_dir(p.parent.name)
        if dir_s == season:
            out.append(f)
            continue
        ep = get_ep(p.name, p.parent.name, allow_bare_ep=True)
        if ep and ep[0] == season:
            out.append(f)
    return out
def _best(files):
    best = files[0]
    for f in files[1:]:
        _tasks.checkpoint()
        if _cmp_versions(f.name, best.name) > 0:
            best = f
    return best.name

def _exempt_hit(name, kws=None):
    if kws is None:
        kws = _exempt_keywords()
    return _dom.exempt_hit(name, kws)

def _nat_key(text):
    """自然排序：Season 9 < Season 10"""
    return _dom.nat_key(text)

def _split_root(path):
    """返回 (库标签, 相对根目录的路径)；不在两个库内则 ('', None)"""
    p = Path(path)
    for root, label in ((_eng().L_ROOT, '本地'), (_eng().S_ROOT, '分享')):
        try:
            return label, p.relative_to(root)
        except ValueError:
            continue
    return '', None

def _exempt_group_of(path, kws):
    """命中白名单的目录层级名，如 综艺/百家讲坛/Season 451/x.strm -> ('百家讲坛', 关键词)。
    只看目录层，不看文件名；找不到返回 (None, None)"""
    _, rel = _split_root(path)
    if rel is None:
        return None, None
    for part in rel.parts[:-1]:
        for k in kws:
            if k in part:
                return part, k
    return None, None

def _share_wins(s_name, l_name):
    """分享版本是否胜出：先看决策模型，再按 7 维对比，全部打平才用平局开关。"""
    return _dom.share_wins(s_name, l_name, _eng()._strategy()['decision'],
                           _cmp_versions, _tie_share_wins())

def _ep_share_ok(s_name, l_name):
    """剧集逐集：分享版是否「达标」——7 维分胜负；全部打平按「平局保留本地/分享」开关。
    （只用于画质优先下的逐集比较；选了「保留本地/保留分享」时剧集在 build 阶段按季直接取舍，不会走到这里。）"""
    return _dom.ep_share_ok(s_name, l_name, _cmp_versions, _tie_share_wins())

def _compare_meta(s_name, l_name, season=False):
    """把「分享版 vs 本地版」逐维对比结果挂到动作上，Web 可直接展示依据。"""
    e = explain_compare(s_name, l_name, _cover_strategy())
    e['share_name'], e['local_name'] = s_name, l_name
    e['decision'] = 'quality_first' if season else _eng()._strategy()['decision']
    e['tie_keep_local'] = not _tie_share_wins()
    return e

def _season_compare(s_files, l_files):
    """
    逐集对齐比较分享与本地同一季（取代双向保留，改为单向择优）。
    同集号多份时取各自最高画质参与比较。规则实现见 domain.governance.season_compare。
    返回 dict：
      s_eps / l_eps : {集号: 最高画质文件}
      s_only / l_only : 各自独有的集号列表（含前序缺失、中间断层）
      common : 交集集号
      s_better : 交集内分享画质 >= 本地的集数
      l_better : 交集内本地画质 > 分享的集数
    """
    return _dom.season_compare(
        s_files, l_files,
        ep_of=_ep,
        cmp=_cmp_versions,
        tie_share_wins=_tie_share_wins(),
        compare_meta=lambda s_name, l_name: _compare_meta(s_name, l_name, season=True),
    )

def _side_gap_desc(keys):
    """生成某侧缺失的具体描述，如：
      '中间断层 (缺 E10，疑似被和谐)' / '缺失前序 (缺 E01-E05)'；完整返回 '完整'。"""
    ok, reason = analyze_season_episodes(keys, '', ())
    return reason

def write_audit_log(category, title, details=None, extra=None):
    try:
        return logger.write(category, title, details, rule_sig=_current_rule_snapshot()['sig'], extra=extra)
    except Exception as e:
        log.warning('审计日志写入失败: %s', e)


AUDIT_PATH_CAP = 60       # 存档摘要每侧最多保留的路径数；完整清单在已执行计划中长期保存
AUDIT_ITEM_CAP = 300      # 单条执行记录最多保留的逐项明细数


def _audit_item(a, result=None):
    """把一个已执行的治理动作整理成可长期保存的结构化明细（供「执行记录」点开看详情）。
    字段与治理清单详情一致（text/reason_label/meta/...），路径列表截断，避免记录无限膨胀。"""
    meta = dict(a.meta or {})
    for k in ('share_paths', 'local_paths', 'members'):
        v = meta.get(k)
        if isinstance(v, list) and len(v) > AUDIT_PATH_CAP:
            meta[k] = v[:AUDIT_PATH_CAP]
            meta['paths_truncated'] = True
    item = {
        'kind': a.kind, 'text': a.text, 'detail': a.detail,
        'reason': meta.get('reason', ''), 'reason_label': meta.get('reason_label', ''),
        'title': meta.get('title', ''), 'season': meta.get('season'),
        'files_count': len(a.files), 'meta': meta,
    }
    if result is not None:
        item['result'] = result
    return item

@dataclass
class Act:
    kind: str
    text: str
    detail: str
    files: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    # 完整执行依据独立于展示 meta；展示路径最多 500 条，不能用作执行门槛。
    guard_paths: list = field(default_factory=list, repr=False)

    @property
    def media_key(self):
        title = self.meta.get('title', '')
        if not title:
            return ''
        season = self.meta.get('season')
        if season is None:
            return f'movie:{title}'
        return f'tv:{title}:S{int(season):02d}'

    @property
    def action_id(self):
        if not self.media_key:
            return ''
        return f'{self.kind}:{self.media_key}'

    @property
    def key(self):
        return self.action_id or f'{self.kind}|{self.text}'

def _media_title_folder(path):
    """从 STRM 路径取得实际媒体标题目录。Season/S00 目录向上一级。"""
    try:
        p = Path(path)
        if parse_season_dir(p.parent.name) is not None:
            return p.parent.parent.name
        return p.parent.name
    except Exception:
        return ''

def _tmdb_ids_from_files(files):
    ids = []
    for f in files or []:
        _tasks.checkpoint()
        folder = _media_title_folder(f)
        m = re.search(r'(?i)tmdb(?:id)?[=\-: ]*(\d+)', folder or '')
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids

def _governance_evidence(title, year, share_files=None, local_files=None):
    """为治理项保留可审计的匹配证据。

    治理匹配的唯一业务依据是「规范化剧名 + 年份」；TMDB 只作为诊断信息。
    如果两边目录写入了不同 TMDB，仍然允许同名同年匹配；如果同一个 TMDB
    同时出现在不同标题/年份，调用方应把它视为元数据冲突，而不是同片依据。
    """
    share_files = list(share_files or [])
    local_files = list(local_files or [])
    sp = sorted({str(Path(f)) for f in share_files})
    lp = sorted({str(Path(f)) for f in local_files})
    st = sorted(set(_tmdb_ids_from_files(share_files)))
    lt = sorted(set(_tmdb_ids_from_files(local_files)))
    return {
        'match_basis': '剧名 + 年份',
        'match_basis_detail': f'规范化剧名「{title}」 + 年份「{year or "未知"}」',
        # 前端默认折叠、点开才显示；全量放行（单季最多几百集），不再截成 20 个
        'share_paths': sp[:500],
        'local_paths': lp[:500],
        'share_count': len(sp),
        'local_count': len(lp),
        'share_tmdb': st,
        'local_tmdb': lt,
        'tmdb': sorted(set(st) | set(lt)),
        'tmdb_consistent': (not st or not lt or bool(set(st) & set(lt))),
    }

def _attach_governance_evidence(meta, title, year, share_files=None, local_files=None):
    m = dict(meta or {})
    ev = _governance_evidence(title, year, share_files, local_files)
    m.update(ev)
    return m

def _act_to_dict(a: Act) -> dict:
    return {
        'text': a.text, 'detail': a.detail,
        'reason': a.meta.get('reason', ''),
        'reason_label': a.meta.get('reason_label', ''),
        'title': a.meta.get('title', ''),
        'season': a.meta.get('season'),
        'meta': a.meta, 'files_count': len(a.files),
    }

def _group_exempt_acts(acts):
    """把白名单豁免项按命中的目录聚合。
    一个节目下有几百个 Season 目录时，只出一条「《百家讲坛》· N 项」，明细放进 meta.members。
    只有单条的分组保持原样输出。"""
    kws = _exempt_keywords()
    groups = {}
    order = []
    for a in acts:
        _tasks.checkpoint()
        title = a.meta.get('title') or ''
        gname = None
        libs = set()
        for i, f in enumerate(a.files):
            _tasks.checkpoint()
            lib, _ = _split_root(f)
            if lib:
                libs.add(lib)
            if gname is None:
                gname, _k = _exempt_group_of(f, kws)
            if (gname or i >= 200) and libs:
                break
        gname = gname or title or a.text
        if gname not in groups:
            groups[gname] = []
            order.append(gname)
        groups[gname].append((a, libs))
    out = []
    for gname in order:
        _tasks.checkpoint()
        items = groups[gname]
        if len(items) == 1:
            out.append(_act_to_dict(items[0][0]))
            continue
        members, kwset, libset, files = [], set(), set(), 0
        for a, libs in items:
            _tasks.checkpoint()
            sn = a.meta.get('season')
            t = a.meta.get('title') or ''
            if re.fullmatch(r'(?i)season\s*\d+', t.strip()):
                tag = t.strip()  # 目录名本身就是「Season 451」，S01 是文件名解析的假季号
            else:
                tag = f'《{t}》' + (f'S{int(sn):02d}' if sn is not None else '')
            if tag not in members:
                members.append(tag)
            kwset.update(a.meta.get('keywords') or [])
            libset |= libs
            files += len(a.files)
        members.sort(key=_nat_key)
        kw_txt = ','.join(sorted(kwset))
        out.append({
            'text': f'🛡️ 《{gname}》 (白名单豁免)',
            'detail': f'命中白名单 [{kw_txt}] ➔ 跳过清理 · 共 {len(members)} 项',
            'reason': 'whitelist', 'reason_label': '白名单豁免',
            'title': gname, 'season': None,
            'meta': {'reason': 'whitelist', 'reason_label': '白名单豁免',
                     'title': gname, 'keywords': sorted(kwset),
                     'libs': sorted(libset), 'member_count': len(members),
                     'members': members[:300]},
            'files_count': files,
        })
    return out

def _ingest_quiet_minutes():
    v = os.environ.get('INGEST_QUIET_MINUTES')  # 环境变量优先（测试用，也便于临时关闭）
    if v is None:
        v = _cfg.load_config().get('ingest_quiet_minutes', '15')
    try:
        m = int(float(str(v).strip()))
    except (TypeError, ValueError):
        m = 15
    return max(0, min(m, 24 * 60))

class _QuietGate:
    def __init__(self, minutes=None):
        self.minutes = _ingest_quiet_minutes() if minutes is None else int(minutes)
        self.cutoff = time.time() - self.minutes * 60
        self._dirs = {}
        self.skipped = 0

    def _dir_recent(self, d):
        v = self._dirs.get(d)
        if v is None:
            try:
                v = os.stat(d).st_mtime >= self.cutoff
            except OSError:
                v = False
            self._dirs[d] = v
        return v

    def skip(self, *file_groups):
        """任一文件所在目录在静默期内有变动 → True（调用方应静默跳过该标题）"""
        if self.minutes <= 0:
            return False
        for files in file_groups:
            _tasks.checkpoint()
            for f in files:
                _tasks.checkpoint()
                if self._dir_recent(os.path.dirname(os.fspath(f))):
                    self.skipped += 1
                    return True
        return False

def _identity_conflicts(S, L):
    """找出同一个 TMDB ID 被不同「剧名+年份」目录使用的情况。

    这是元数据诊断，不参与自动删除：TMDB 可能被整理器误写，程序不能仅凭
    一个冲突 ID 猜哪一边应该删。用户可在治理详情里看到双方路径后人工处理。
    """
    by_tmdb = defaultdict(list)
    for lib_name, lib in (('local', L), ('share', S)):
        _tasks.checkpoint()
        for tid_full, keys in lib.tmdb_refs.items():
            # tid_full 形如 'movie:103' / 'tv:103'：电影与剧集是独立 ID 空间，分开聚合
            _tasks.checkpoint()
            kind, _, tid = tid_full.partition(':')
            for key in keys:
                _tasks.checkpoint()
                disp, base, year = lib.meta.get(key, ('', '', None))
                by_tmdb[tid_full].append({'lib': lib_name, 'title': disp, 'base': base,
                                          'year': year, 'key': key, 'kind': kind, 'tmdb': tid})
    out = []
    for tid_full, rows in sorted(by_tmdb.items()):
        _tasks.checkpoint()
        identities = {(r['base'].casefold(), r.get('year') or '') for r in rows}
        if len(identities) <= 1:
            continue
        # 同一身份在双库出现不算冲突；只有一个 TMDB 对应多个不同身份才报告。
        first = rows[0]
        out.append({'tmdb': first.get('tmdb', ''), 'kind': first.get('kind', 'movie'),
                    'count': len(rows), 'identities': [
            {'title': r['title'], 'year': r.get('year'), 'lib': r['lib']} for r in rows
        ]})
    return out

def _build_plan_with_libs():
    """生成治理计划，同时把本次用到的两个 _eng().Lib 一并返回，
    供调用方直接取 strm_count 等数据，避免为了计数再对双库做一遍全量 rglob。"""
    t_start = time.time()
    # 两个库互不相干，并行遍历：磁盘/网络挂载慢时能把等待时间叠在一起
    with ThreadPoolExecutor(max_workers=2) as _ex:
        task = _tasks.current_task()
        _fs = _ex.submit(_tasks.run_in_task, task, _eng()._get_lib, _eng().S_ROOT)
        _fl = _ex.submit(_tasks.run_in_task, task, _eng()._get_lib, _eng().L_ROOT)
        S, L = _fs.result(), _fl.result()
    log.info('双库遍历完成：分享 %d / 本地 %d 个 STRM，耗时 %.1fs',
             S.strm_count, L.strm_count, time.time() - t_start)
    s = _eng()._strategy()
    kws = _exempt_keywords()
    gate = _QuietGate()
    acts = []

    for key, s_files in S.mov.items():
        _tasks.checkpoint()
        lk = _eng()._match_governance_key(S, L, key, L.mov, s.get('match_strategy', 'title_year'), 'movie')
        if not lk: continue
        # 保险：同一身份在任一库里有剧集分集时，说明这是一部剧，
        # 剩下的无季集文件(花絮等)不能按电影整体比画质/删除，交给剧集分支。
        if key in S.tv or lk in L.tv:
            continue
        disp, l_files = S.meta[key][0], L.mov[lk]

        if gate.skip(s_files, l_files):  # 入库未满静默期：暂不治理
            continue

        hit_kws = _exempt_hit(disp, kws)
        if not hit_kws:
            for files in (s_files, l_files):
                _tasks.checkpoint()
                for f in files:
                    _tasks.checkpoint()
                    hit_kws = _exempt_hit(str(f), kws)
                    if hit_kws: break
                if hit_kws: break
        if hit_kws:
            acts.append(Act('exempt', f'🛡️ 《{disp}》 (白名单豁免)',
                            f'├─ 🛡️ 《{disp}》: 命中白名单 [{",".join(hit_kws)}] ➔ 跳过清理',
                            s_files + l_files,
                            meta={'reason': 'whitelist', 'reason_label': '白名单豁免',
                                  'title': disp, 'keywords': hit_kws}))
            continue

        s_best, l_best = _best(s_files), _best(l_files)
        if _share_wins(s_best, l_best):
            acts.append(Act('loc', f'🎬 《{disp}》 (分享画质达标，穿透删本地腾网盘)',
                            f'├─ 🎬 《{disp}》: 分享画质达标 ➔ CD2联动删除115网盘旧源', l_files,
                            meta={'reason': 'share_better', 'reason_label': '分享画质达标', 'title': disp,
                                  'compare': _compare_meta(s_best, l_best)}))
        else:
            acts.append(Act('shr', f'🎬 《{disp}》 (分享画质劣于本地，淘汰分享)',
                            f'├─ 🎬 《{disp}》: 分享画质次级 ➔ 清理分享影视库strm', s_files,
                            meta={'reason': 'local_better', 'reason_label': '本地画质更优', 'title': disp,
                                  'compare': _compare_meta(s_best, l_best)}))

    for key, s_seasons in S.tv.items():
        _tasks.checkpoint()
        disp = S.meta[key][0]
        lk = _eng()._match_governance_key(S, L, key, L.tv, s.get('match_strategy', 'title_year'), 'tv')
        l_seasons = L.tv[lk] if lk else {}

        # 入库未满静默期：只要任意一季（任一侧）刚有变动，整部剧暂不治理，
        # 避免"还没入完"的剧被当成缺集/落后处理
        if gate.skip(*s_seasons.values(), *l_seasons.values()):
            continue

        hit_kws = _exempt_hit(disp, kws)
        if not hit_kws:
            for files_map in (s_seasons, l_seasons):
                _tasks.checkpoint()
                for files in files_map.values():
                    _tasks.checkpoint()
                    for f in files:
                        _tasks.checkpoint()
                        hit_kws = _exempt_hit(str(f), kws)
                        if hit_kws: break
                    if hit_kws: break
                if hit_kws: break
        if hit_kws:
            for sn, s_files in sorted(s_seasons.items()):
                _tasks.checkpoint()
                tag = f'《{disp}》S{sn:02d}'
                acts.append(Act('exempt', f'🛡️ {tag} (白名单豁免)',
                                f'├─ 🛡️ {tag}: 命中白名单 [{",".join(hit_kws)}] ➔ 跳过清理',
                                s_files,
                                meta={'reason': 'whitelist', 'reason_label': '白名单豁免',
                                      'title': disp, 'season': sn, 'keywords': hit_kws}))
            continue

        s00_files = s_seasons.get(0)
        l00_files = l_seasons.get(0)
        special_action = s.get('special_action', 'compare')
        # 多季保护「开启」+ 特别篇「画质对比」+ 画质优先：S00 算作一季，参与多季保护（不再单独比画质，
        # 否则分享的 S00 画质好就会把本地合集的 S00 掏空）。
        protect_s00 = _dom.protect_s00(s['multi_season_protect'], special_action, s['decision'],
                                       l_seasons, s_seasons)

        if special_action == 'delete':
            # 清理档：本地+分享都删
            tag = f'《{disp}》S00'
            if l00_files:
                acts.append(Act('loc', f'🎬 {tag} (特别篇清理 → 删本地)',
                                f'├─ 🎬 {tag}: 特别篇清理 ➔ CD2联动删除115网盘旧源',
                                l00_files,
                                meta={'reason': 'special_delete_local',
                                      'reason_label': '特别篇清理-删本地',
                                      'title': disp, 'season': 0}))
            if s00_files:
                acts.append(Act('shr', f'🎬 {tag} (特别篇清理 → 删分享)',
                                f'├─ 🎬 {tag}: 特别篇清理 ➔ 清理分享影视库strm',
                                s00_files,
                                meta={'reason': 'special_delete_share',
                                      'reason_label': '特别篇清理-删分享',
                                      'title': disp, 'season': 0}))
        elif special_action != 'ignore' and s00_files and l00_files and not protect_s00:
            # 画质对比档（一边没有时不做处理）
            s_best, l_best = _best(s00_files), _best(l00_files)
            tag = f'《{disp}》S00'
            if _share_wins(s_best, l_best):
                acts.append(Act('loc', f'🎬 {tag} (特别篇画质对比 → 删本地)',
                                f'├─ 🎬 {tag}: 分享画质达标 ➔ CD2联动删除115网盘旧源',
                                l00_files,
                                meta={'reason': 'special_share_better',
                                      'reason_label': '特别篇分享更优',
                                      'title': disp, 'season': 0,
                                      'compare': _compare_meta(s_best, l_best)}))
            else:
                acts.append(Act('shr', f'🎬 {tag} (特别篇画质对比 → 删分享)',
                                f'├─ 🎬 {tag}: 本地画质更优 ➔ 清理分享影视库strm',
                                s00_files,
                                meta={'reason': 'special_local_better',
                                      'reason_label': '特别篇本地更优',
                                      'title': disp, 'season': 0,
                                      'compare': _compare_meta(s_best, l_best)}))
        # ignore: 完全跳过

        _lo = 0 if protect_s00 else 1   # protect_s00 时 S00 也进入季集合
        s_proper = {k: v for k, v in s_seasons.items() if k >= _lo}
        l_proper = {k: v for k, v in l_seasons.items() if k >= _lo}

        # ── 多季保护两档：off / compare ──
        multi_protect = s['multi_season_protect']
        n_local = len(l_proper)
        is_multi_local = n_local >= 2
        n_share = len(s_proper)
        is_multi_share = n_share >= 2

        # 决策模型「保留本地 / 保留分享」：剧集逐季直接按决策取舍，不比画质，优先级高于多季保护。只处理两库都有的季；单边独有的季是补充，不动。
        decision = s['decision']
        if decision in ('keep_local', 'keep_share'):
            for sn, s_files in sorted(s_proper.items()):
                _tasks.checkpoint()
                if sn not in l_proper:
                    continue
                l_files = l_proper[sn]
                tag = f'《{disp}》S{sn:02d}'
                if decision == 'keep_local':
                    acts.append(Act('shr', f'📺 {tag} (保留本地 → 淘汰分享)',
                                    f'├─ 📺 {tag}: 决策「保留本地」 ➔ 清理分享影视库strm',
                                    s_files,
                                    meta={'reason': 'decision_keep_local',
                                          'reason_label': '保留本地-删分享',
                                          'title': disp, 'season': sn}))
                    continue
                # keep_share 会通过 CD2 删 115 源，不可恢复：分享这一季没覆盖本地全部集时不删本地
                cmp = _season_compare(s_files, l_files)
                if not _dom.share_covers_local(cmp):
                    continue
                acts.append(Act('loc', f'📺 {tag} (保留分享 → 删本地腾网盘)',
                                f'├─ 📺 {tag}: 决策「保留分享」 ➔ CD2联动删除115网盘旧源',
                                l_files,
                                meta={'reason': 'decision_keep_share',
                                      'reason_label': '保留分享-删本地',
                                      'title': disp, 'season': sn}))
            continue

        # compare 档（即“开启”）：本地多季时，仅当分享对本地全部季逐集达标才整体删本地；
        # 否则逐季独立择优（保护本地不被部分季掏空）。
        if multi_protect == 'compare' and (is_multi_local or is_multi_share):
            _emit_multi_protect_compare(acts, disp, s_proper, l_proper, n_local, n_share)
            continue

        # off 档：逐季独立择优
        for sn, s_files in sorted(s_proper.items()):
            _tasks.checkpoint()
            tag = f'《{disp}》S{sn:02d}'
            # 分享独有 / 本地独有：静默，不生成 action（主力是分享，列表不宜过多）
            if sn not in l_proper:
                continue
            l_files = l_proper[sn]
            _emit_season_act(acts, disp, sn, s_files, l_files)

    # ── 库内多版本去重：同片/同集存在多份 strm 时，只留画质最优的一份 ──
    _dedupe_lib_versions(acts, L, S, gate, kws)
    _eng()._QUIET_LAST['n'] = gate.skipped
    # 为每一条治理动作补齐「为什么匹配到」的证据，供 Web 详情/审计使用。
    # 这里不改变决策结果，只增加可追溯信息。
    #
    # 性能注意：旧实现对「每一条动作」都完整遍历 S.meta / L.meta，再把同标题
    # 的所有文件重新聚合；动作多时会变成 O(动作数 × 标题数)，大库上会把一次
    # 15 秒级扫描拖到分钟级。这里先建立标题索引，再按标题 O(1) 取文件。
    def _evidence_index(lib):
        idx = defaultdict(list)
        for key, mm in lib.meta.items():
            _tasks.checkpoint()
            disp, base, _year = mm
            if key in lib.mov:
                files = lib.mov[key]
            elif key in lib.tv:
                files = [f for sm in lib.tv[key].values() for f in sm]
            else:
                continue
            idx[disp].extend(files)
            idx[base].extend(files)
        return idx

    S_evidence = _evidence_index(S)
    L_evidence = _evidence_index(L)
    evidence_cache = {}

    # 按实际治理身份构建关联组，避免译名/TMDB 配对时按展示标题找错保留依据。
    groups, path_group, peers = {}, {}, defaultdict(set)
    for lib, side in ((S, 'shr'), (L, 'loc')):
        _tasks.checkpoint()
        for media, items in (('movie', lib.mov), ('tv', lib.tv)):
            _tasks.checkpoint()
            for key, value in items.items():
                _tasks.checkpoint()
                files = list(value) if media == 'movie' else [f for fs in value.values() for f in fs]
                group = (side, media, key)
                groups[group] = files
                for f in files:
                    _tasks.checkpoint()
                    path_group[str(f)] = group
    for media, items, local_items in (('movie', S.mov, L.mov), ('tv', S.tv, L.tv)):
        _tasks.checkpoint()
        for key in items:
            _tasks.checkpoint()
            lk = _eng()._match_governance_key(S, L, key, local_items, s.get('match_strategy', 'title_year'), media)
            if lk:
                sg, lg = ('shr', media, key), ('loc', media, lk)
                peers[sg].add(lg); peers[lg].add(sg)

    for a in acts:
        _tasks.checkpoint()
        if a.kind not in ('loc', 'shr', 'keep', 'exempt'):
            continue
        if a.kind in ('loc', 'shr'):
            related = set()
            for f in a.files:
                _tasks.checkpoint()
                group = path_group.get(str(f))
                if group:
                    related.add(group)
                    related.update(peers[group])
            a.guard_paths = sorted({str(f) for group in related for f in groups[group]})
        title = a.meta.get('title') or ''
        year = a.meta.get('year')
        cache_key = (title, year)
        cached = evidence_cache.get(cache_key)
        if cached is None:
            sf = list(dict.fromkeys(S_evidence.get(title, ())))
            lf = list(dict.fromkeys(L_evidence.get(title, ())))
            # 兼容历史动作标题带年份括号的情况。
            if not sf and not lf:
                base_title = re.sub(r'\s*\(\d{4}\)$', '', title).strip()
                sf = list(dict.fromkeys(S_evidence.get(base_title, ())))
                lf = list(dict.fromkeys(L_evidence.get(base_title, ())))
            if not year:
                for f in (sf + lf + list(a.files)):
                    _tasks.checkpoint()
                    folder = _media_title_folder(f)
                    y = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)', folder or '')
                    if y:
                        year = y.group(1); break
            cached = (sf, lf, year)
            evidence_cache[cache_key] = cached
        sf, lf, year = cached
        _sn = a.meta.get('season')
        if _sn is not None:
            sf = _filter_evidence_by_season(sf, _sn)
            lf = _filter_evidence_by_season(lf, _sn)
        a.meta = _attach_governance_evidence(
            a.meta, title, year,
            sf or ([f for f in a.files] if a.kind == 'shr' else []),
            lf or ([f for f in a.files] if a.kind == 'loc' else []))
        if s.get('match_strategy', 'title_year') == 'tmdb_first':
            a.meta['identity_source'] = 'tmdb_first'
            a.meta['tmdb_role'] = '优先匹配键；同 TMDB 多身份列为冲突不处理'
            a.meta['match_basis'] = 'TMDB 优先（冲突/缺失回退剧名+年份）'
        else:
            a.meta['identity_source'] = 'filesystem:title+year'
            a.meta['tmdb_role'] = '辅助元数据，不参与跨标题匹配'

    if gate.skipped:
        log.info('入库未满 %d 分钟，静默跳过 %d 个标题（不进入本次治理队列）', gate.minutes, gate.skipped)
    log.info('治理计划生成完成：%d 项，总耗时 %.1fs', len(acts), time.time() - t_start)
    return acts, S, L

def build_plan():
    return _build_plan_with_libs()[0]

def _dedupe_lib_versions(acts, L, S, gate=None, kws=None):
    """库内多版本去重（洗版/追更残留），只在本库内部对比，不跨库拆分：
      - 电影：同一 key（同 tmdb）下多份 strm → 留最优一份；
      - 剧集：同一季内同一集号多份 strm → 每集各留最优一份。
    已被库间治理覆盖（整组删除）的标题跳过；白名单跳过；S00 不参与。
    本地低版本走 CD2 联动删源（腾空间），分享低版本直接删 strm。
    不同 tmdb（如正片 vs 导演版）是不同 key，天然互不影响。"""
    if kws is None:
        kws = _exempt_keywords()
    if gate is None:
        gate = _QuietGate()
    covered = {(a.kind, a.media_key) for a in acts if a.kind in ('loc', 'shr')}
    for lib, kind in ((L, 'loc'), (S, 'shr')):
        # 电影
        _tasks.checkpoint()
        for key, files in lib.mov.items():
            _tasks.checkpoint()
            if len(files) < 2:
                continue
            disp = lib.meta[key][0]
            if key in lib.tv:  # 同身份下有剧集分集：这是剧，不按「同片多版本」去重
                continue
            if gate.skip(files):  # 入库未满静默期
                continue
            # 白名单：标题或任一文件路径命中都豁免（Season NNN 类目录标题不含关键字，必须查路径）
            if _exempt_hit(disp, kws) or any(_exempt_hit(str(f), kws) for f in files):
                continue
            if (kind, f'movie:{disp}') in covered:
                continue
            best = files[0]
            for f in files[1:]:
                _tasks.checkpoint()
                if _cmp_versions(f.name, best.name) > 0:
                    best = f
            losers = [f for f in files if f is not best]
            cloud_note = 'CD2联动删除115网盘低版本源' if kind == 'loc' else '清理分享影视库低版本strm'
            acts.append(Act(kind, f'🎬 《{disp}》 (同片多版本 {len(files)} 份 → 留最优删其余)',
                            f'├─ 🎬 《{disp}》: 库内多版本去重（保留 {quality_label(best.name)}） ➔ {cloud_note}',
                            losers,
                            meta={'reason': 'dup_version_local' if kind == 'loc' else 'dup_version_share',
                                  'reason_label': '库内多版本-删低版',
                                  'title': disp,
                                  'keep_label': quality_label(best.name),
                                  'dup_count': len(files)}))
        # 剧集（S00 不参与，特别篇有独立策略）
        for key, seasons in lib.tv.items():
            _tasks.checkpoint()
            disp = lib.meta[key][0]
            all_files = [f for fl in seasons.values() for f in fl]
            if gate.skip(all_files):  # 入库未满静默期
                continue
            if _exempt_hit(disp, kws) or any(_exempt_hit(str(f), kws) for f in all_files):
                continue
            for sn, files in seasons.items():
                _tasks.checkpoint()
                if sn <= 0 or len(files) < 2:
                    continue
                by_ep = defaultdict(list)
                for f in files:
                    _tasks.checkpoint()
                    ep = _ep(f)
                    if not ep or ep[0] <= 0:
                        continue
                    by_ep[ep[1]].append(f)
                dup_eps = {e: fs for e, fs in by_ep.items() if len(fs) > 1}
                if not dup_eps:
                    continue
                mk = f'tv:{disp}:S{int(sn):02d}'
                if (kind, mk) in covered:
                    continue
                losers = []
                kept_labels = []
                for e, fs in sorted(dup_eps.items()):
                    _tasks.checkpoint()
                    best = fs[0]
                    for f in fs[1:]:
                        _tasks.checkpoint()
                        if _cmp_versions(f.name, best.name) > 0:
                            best = f
                    losers.extend(f for f in fs if f is not best)
                    kept_labels.append(f'E{e:02d}留{quality_label(best.name)}')
                cloud_note = 'CD2联动删除115网盘低版本源' if kind == 'loc' else '清理分享影视库低版本strm'
                tag = f'《{disp}》S{sn:02d}'
                acts.append(Act(kind, f'📺 {tag} (同集多版本 {len(dup_eps)} 集 → 各留最优)',
                                f'├─ 📺 {tag}: 库内多版本去重（{"；".join(kept_labels[:5])}） ➔ {cloud_note}',
                                losers,
                                meta={'reason': 'dup_version_local' if kind == 'loc' else 'dup_version_share',
                                      'reason_label': '库内多版本-删低版',
                                      'title': disp, 'season': sn,
                                      'dup_eps': len(dup_eps),
                                      'dup_count': len(files)}))

def _emit_season_act(acts, disp, sn, s_files, l_files):
    """单季治理：先判完整性（残次品），完整才逐集择优。

    决策规则（实现见 domain.governance.season_verdict）：
      - 合并后集号不完整（前序缺 1 或中间断层）→ 视为残次品，谁不完整删谁。
      - 完整：交集内逐集比画质，达标率高者保留，另一方淘汰。
    本函数只负责按裁决结果构造 Act（文案 / meta）。
    """
    tag = f'《{disp}》S{sn:02d}'
    cmp = _season_compare(s_files, l_files)
    v = _dom.season_verdict(cmp, bool(l_files), bool(s_files), _replace_ratio())
    s_total = len(cmp['s_eps'])
    l_total = len(cmp['l_eps'])
    common = len(cmp['common'])
    s_better = cmp['s_better']
    l_better = cmp['l_better']

    # ── 残次品：本地、分享各自独立判定，谁不完整删谁，并写明具体缺什么 ──
    if v['residual_local']:
        gap = _side_gap_desc(cmp['l_eps'].keys())
        acts.append(Act('loc', f'⚠️ {tag} (残次品：本地{gap} → 删本地)',
                        f'├─ ⚠️ {tag}: 本地{gap} ➔ CD2联动删除115网盘旧源',
                        l_files,
                        meta={'reason': 'incomplete_delete_local',
                              'reason_label': '残次品-删本地',
                              'title': disp, 'season': sn}))
    if v['residual_share']:
        gap = _side_gap_desc(cmp['s_eps'].keys())
        acts.append(Act('shr', f'⚠️ {tag} (残次品：分享{gap} → 删分享)',
                        f'├─ ⚠️ {tag}: 分享{gap} ➔ 清理分享影视库strm',
                        s_files,
                        meta={'reason': 'incomplete_delete_share',
                              'reason_label': '残次品-删分享',
                              'title': disp, 'season': sn}))
    if v['outcome'] in ('incomplete', 'no_common'):
        # incomplete：只处理残次品；no_common：无交集集号无法逐集比画质 → 静默，不删任何一边
        return

    # 分享在交集内的达标率
    share_ratio = s_better / common if common else 0.0
    outcome = v['outcome']

    # 分享集数领先（含独有集）且达标率够高 → 删本地，留分享
    if outcome == 'share_wins':
        acts.append(Act('loc', f'📺 {tag} (分享画质达标 {s_better}/{common} 集 → 删本地腾网盘)',
                        f'├─ 📺 {tag}: 分享达标率 {share_ratio:.0%} ({s_better}/{common}集) ➔ CD2联动删除115网盘旧源',
                        l_files,
                        meta={'reason': 'share_wins',
                              'reason_label': '分享择优-删本地',
                              'title': disp, 'season': sn,
                              'share_better': s_better, 'local_better': l_better,
                              'share_total': s_total, 'local_total': l_total,
                              'compare': cmp['sample']}))
    else:
        if outcome == 'local_wins_fewer':
            # 分享在共同集上画质虽好，但集数明显少于本地 → 按集数优先规则保留本地
            acts.append(Act('shr', f'📺 {tag} (分享仅 {s_total} 集、本地 {l_total} 集 → 淘汰分享保留本地)',
                            f'├─ 📺 {tag}: 共同 {common} 集中分享优 {s_better} 集，但本地集数更全 ➔ 清理分享影视库strm',
                            s_files,
                            meta={'reason': 'local_wins',
                                  'reason_label': '本地更全-删分享',
                                  'title': disp, 'season': sn,
                                  'share_better': s_better, 'local_better': l_better,
                                  'share_total': s_total, 'local_total': l_total,
                                  'compare': cmp['sample']}))
        else:
            acts.append(Act('shr', f'📺 {tag} (本地更优 {l_better}/{common} 集 → 淘汰分享)',
                            f'├─ 📺 {tag}: 本地达标率 {(l_better/common):.0%} ({l_better}/{common}集) ➔ 清理分享影视库strm',
                            s_files,
                            meta={'reason': 'local_wins',
                                  'reason_label': '本地择优-删分享',
                                  'title': disp, 'season': sn,
                                  'share_better': s_better, 'local_better': l_better,
                                  'share_total': s_total, 'local_total': l_total,
                                  'compare': cmp['sample']}))

def _emit_season_acts_full(acts, disp, s_proper, l_proper, n_local):
    """多季保护 compare 档（开启）：两阶段治理。

    第一阶段：残次品治理（先剔除不完整）——本地/分享各自的残次品季（前序缺失/中间断层）
    已在此前的单季治理中处理，此处仅随多季保护结果一并呈现原因。

    第二阶段：多季保护（在剩余「完整季」上做门槛判定）——
      分享覆盖了本地全部剩余季？
      ├─ 否（未全覆盖）→ 分享这些完整季全部淘汰，不比画质（达不到替换门槛）；
      └─ 是（全覆盖且全部达标）→ 整体删本地（整剧零和，腾网盘）。
    进入第二阶段的东西已无缺集问题，故不存在「全覆盖后仍缺集」的情况。
    """
    all_pass = _dom.multi_all_pass(l_proper, s_proper, _season_compare, _replace_ratio())

    if all_pass:
        for sn, l_files in sorted(l_proper.items()):
            _tasks.checkpoint()
            tag = f'《{disp}》S{sn:02d}'
            acts.append(Act('loc', f'📺 {tag} (整剧零和：分享全季达标 → 删本地)',
                            f'├─ 📺 {tag}: 整剧零和 ➔ CD2联动删除115网盘旧源', l_files,
                            meta={'reason': 'multi_full_share',
                                  'reason_label': '整剧零和-分享替代',
                                  'title': disp, 'season': sn,
                                  'local_seasons': n_local,
                                  'share_seasons': len(s_proper)}))
        return 'share'

    # 未全覆盖（或存在单季问题）→ 分享这些季全部淘汰，不比画质。
    # 注：残次品在第一阶段已独立治理（本地不完整删本地），此处随之呈现原因。
    for sn, s_files in sorted(s_proper.items()):
        _tasks.checkpoint()
        if sn not in l_proper:
            continue  # 分享独有季（本地没有）→ 静默，不在多季保护对比范围
        cmp = _season_compare(s_files, l_proper[sn])
        common = len(cmp['common'])
        tag = f'《{disp}》S{sn:02d}'
        # 本地残次品 → 删本地（残次品第一阶段治理，与多季保护无关）
        if _dom.residual_flags(cmp, bool(l_proper[sn]), False)[0]:
            gap = _side_gap_desc(cmp['l_eps'].keys())
            acts.append(Act('loc', f'⚠️ {tag} (残次品：本地{gap} → 删本地)',
                            f'├─ ⚠️ {tag}: 本地{gap} ➔ CD2联动删除115网盘旧源',
                            l_proper[sn],
                            meta={'reason': 'incomplete_delete_local',
                                  'reason_label': '残次品-删本地',
                                  'title': disp, 'season': sn}))
        # 收集该季的所有治理原因（原因全列：残次品 / 集数不足 / 画质次级）
        reasons = _dom.partial_share_reasons(cmp, _side_gap_desc)
        # 未全覆盖 → 淘汰分享（本地不动）
        detail_parts = ['多季保护：分享未全覆盖本地（不替换）']
        if reasons:
            detail_parts.append('；'.join(reasons))
        acts.append(Act('shr', f'📺 {tag} (多季保护·分享未全覆盖 → 淘汰分享)',
                        f'├─ 📺 {tag}: {"；".join(detail_parts)} ➔ 清理分享影视库strm',
                        s_files,
                        meta={'reason': 'multi_protect_partial_share',
                              'reason_label': '多季保护-淘汰分享',
                              'title': disp, 'season': sn,
                              'reasons': reasons,
                              'local_seasons': n_local,
                              'share_seasons': len(s_proper)}))
    return 'local'

def _emit_season_residuals(acts, disp, sn, cmp, s_files, l_files):
    """残次品独立判定（与多季保护方向无关）：谁不完整删谁。返回是否产生了残次品动作。"""
    tag = f'《{disp}》S{sn:02d}'
    hit = False
    res_local, res_share = _dom.residual_flags(cmp, bool(l_files), bool(s_files))
    if res_local:
        gap = _side_gap_desc(cmp['l_eps'].keys())
        acts.append(Act('loc', f'⚠️ {tag} (残次品：本地{gap} → 删本地)',
                        f'├─ ⚠️ {tag}: 本地{gap} ➔ CD2联动删除115网盘旧源',
                        l_files,
                        meta={'reason': 'incomplete_delete_local',
                              'reason_label': '残次品-删本地',
                              'title': disp, 'season': sn}))
        hit = True
    if res_share:
        gap = _side_gap_desc(cmp['s_eps'].keys())
        acts.append(Act('shr', f'⚠️ {tag} (残次品：分享{gap} → 删分享)',
                        f'├─ ⚠️ {tag}: 分享{gap} ➔ 清理分享影视库strm',
                        s_files,
                        meta={'reason': 'incomplete_delete_share',
                              'reason_label': '残次品-删分享',
                              'title': disp, 'season': sn}))
        hit = True
    return hit


def _emit_s00_follow(acts, disp, files, side, reason, label, head):
    """S00（特别篇）跟随该剧多季保护的结果：side='loc' 删本地 S00，'shr' 删分享 S00。"""
    tag = f'《{disp}》S00'
    if side == 'loc':
        acts.append(Act('loc', f'🎬 {tag} ({head} → 淘汰本地)',
                        f'├─ 🎬 {tag}: {head} ➔ CD2联动删除115网盘旧源', files,
                        meta={'reason': reason, 'reason_label': label, 'title': disp, 'season': 0}))
    else:
        acts.append(Act('shr', f'🎬 {tag} ({head} → 淘汰分享)',
                        f'├─ 🎬 {tag}: {head} ➔ 清理分享影视库strm', files,
                        meta={'reason': reason, 'reason_label': label, 'title': disp, 'season': 0}))


def _emit_multi_protect_compare(acts, disp, s_proper, l_proper, n_local, n_share):
    """多季保护「开启」档：画质对比照常进行，多季保护只负责「不让任何一边被掏空」。

      - 两边季集合完全相同（每一季都有对应）→ 单库 vs 单库整体比画质、留优者：
        分享逐季逐集全部达标 → 整体删本地；否则整体淘汰分享（_emit_season_acts_full，原有逻辑）；
        整边删除不会让任何一库出现断层，所以允许比画质；
      - 本地季更多（分享的季是本地的子季）→ 保护本地合集：分享同季副本淘汰，不比画质；
      - 分享季更多（本地的季是分享的子季）→ 对称，保护分享合集：本地同季副本淘汰，不比画质；
      - 双方各有对方没有的季（部分重叠）→ 删任何一边都会让该库断层，同季两边都不动，只处理各自的残次品。
    S00（特别篇）若在 s_proper / l_proper 里，算作一季参与上面的包含关系判断，
    它的处理跟随其余季的结果（不单独比画质，不参与完整性 / 逐集对照）。
    单季剧不会进入这里（调用处要求至少一边 ≥2 季），仍走逐集完整性 / 画质逻辑。
    """
    mode = _dom.multi_protect_mode(s_proper, l_proper)
    sp = {k: v for k, v in s_proper.items() if k > 0}
    lp = {k: v for k, v in l_proper.items() if k > 0}
    s0, l0 = s_proper.get(0), l_proper.get(0)
    if mode == 'share_protect':
        _emit_share_protect_acts(acts, disp, sp, lp, n_share)
        if s0 and l0:
            _emit_s00_follow(acts, disp, l0, 'loc', 'multi_protect_partial_local',
                             '多季保护-淘汰本地', '多季保护·分享合集优先')
    elif mode == 'full':
        winner = _emit_season_acts_full(acts, disp, sp, lp, n_local)
        if s0 and l0:
            if winner == 'share':
                _emit_s00_follow(acts, disp, l0, 'loc', 'multi_full_share',
                                 '整剧零和-分享替代', '整剧零和：分享全季达标')
            else:
                _emit_s00_follow(acts, disp, s0, 'shr', 'multi_protect_partial_share',
                                 '多季保护-淘汰分享', '多季保护·本地合集优先')
    else:
        for sn in sorted(set(sp) & set(lp)):
            _tasks.checkpoint()
            cmp = _season_compare(sp[sn], lp[sn])
            _emit_season_residuals(acts, disp, sn, cmp, sp[sn], lp[sn])


def _emit_share_protect_acts(acts, disp, s_proper, l_proper, n_share):
    """分享覆盖本地（或分享才是多季合集）时保护分享，本地同季副本纳入清理。

    与 _emit_season_acts_full（保护本地）镜像：
      - 两库同季：不比画质，本地副本纳入清理；分享独有季 / 本地独有季：补充，不动；
      - 残次品仍各自独立判定（谁不完整删谁），两边都完整才进入「删本地」；
      - 两边都完整但分享该季集数达不到本地 → 按原有规则淘汰分享（本地更全），不删本地。
    """
    for sn, l_files in sorted(l_proper.items()):
        _tasks.checkpoint()
        if sn not in s_proper:
            continue
        s_files = s_proper[sn]
        cmp = _season_compare(s_files, l_files)
        tag = f'《{disp}》S{sn:02d}'

        if _emit_season_residuals(acts, disp, sn, cmp, s_files, l_files):
            continue

        verdict = _dom.share_protect_verdict(cmp)
        if verdict == 'skip':
            continue
        if verdict == 'share_fewer':
            # 分享这一季集数达不到本地 → 按原有规则淘汰分享（本地更全），不删本地
            acts.append(Act('shr', f'📺 {tag} (分享仅 {len(cmp["s_eps"])} 集、本地 {len(cmp["l_eps"])} 集 → 淘汰分享保留本地)',
                            f'├─ 📺 {tag}: 分享集数不足本地 ➔ 清理分享影视库strm',
                            s_files,
                            meta={'reason': 'local_wins', 'reason_label': '本地更全-删分享',
                                  'title': disp, 'season': sn,
                                  'share_total': len(cmp['s_eps']), 'local_total': len(cmp['l_eps'])}))
            continue

        reason, label, head = 'multi_protect_partial_local', '多季保护-淘汰本地', '多季保护·分享合集优先'
        acts.append(Act('loc', f'📺 {tag} ({head} → 淘汰本地)',
                        f'├─ 📺 {tag}: {head}，分享 {n_share} 季合集保持完整 ➔ CD2联动删除115网盘旧源',
                        l_files,
                        meta={'reason': reason, 'reason_label': label,
                              'title': disp, 'season': sn,
                              'local_seasons': len(l_proper),
                              'share_seasons': n_share}))

def _current_rule_snapshot():
    """返回当前治理相关规则及稳定指纹。所有扫描/计划/执行记录共用这一口径。"""
    cfg = _cfg.load_config()
    strategy = _cfg.get_strategy()
    rule_keys = ('strategy_decision','strategy_multi_season_protect','strategy_tie_keep_local',
                 'strategy_exempt_keywords','strategy_special_action','ingest_quiet_minutes',
                 'ingest_interval_min','ingest_enabled')
    rules = {k: cfg.get(k, '') for k in rule_keys}
    rules['strategy'] = strategy
    # 七维画质规则同样决定删除方向。指纹必须覆盖顺序、开关、档位和发布组，
    # 否则修改画质设置后，旧计划仍会通过执行前的规则校验。
    # 只取规范化后的业务字段，排除文案以及 JSON 缩进等非业务变化。
    rules['cover_strategy'] = {'rules': [
        {k: r[k] for k in ('key', 'enabled', 'tiers', 'groups')}
        for r in _cfg.get_cover_strategy()['rules']
    ]}
    raw = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    sig = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    return {'sig': sig, 'rules': rules}

def _execution_snapshots(todo):
    """Capture only plan-related STRMs once, sharing fingerprints across actions."""
    deleting = {str(f) for a in todo for f in a.files}
    snapshots, guards = {}, []
    roots = (_eng().L_ROOT, _eng().S_ROOT)
    for a in todo:
        _tasks.checkpoint()
        targets = sorted({str(f) for f in a.files})
        retained = sorted(set(a.guard_paths) - deleting)
        for path in targets + retained:
            _tasks.checkpoint()
            if path not in snapshots:
                snapshots[path] = _plan_guard.fingerprint(path, roots)
        guard = {'version': _plan_guard.VERSION, 'targets': targets, 'retained': retained}
        # 补集可能让“残次品”变完整；此类动作还需核对关联季目录的 STRM 集合。
        if str(a.meta.get('reason') or '').startswith('incomplete_delete_'):
            guard['directories'] = {d: _plan_guard.directory_strms(d)
                for d in sorted({str(Path(p).parent) for p in targets})}
        guards.append(guard)
    return snapshots, guards


def save_plan(acts):
    todo = [a for a in acts if a.kind not in ('keep', 'exempt') and a.action_id]
    if not todo:
        _tasks.begin_publication()
        return None
    _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    ids = sorted(a.action_id for a in todo)
    pid = hashlib.md5(('\n'.join(ids) + str(ts)).encode()).hexdigest()[:8]
    actions_payload = []
    snapshots, guards = _execution_snapshots(todo)
    for a, guard in zip(todo, guards):
        actions_payload.append({
            'action_id': a.action_id,
            'media_key': a.media_key,
            'kind': a.kind,
            'text': a.text,
            'reason': a.meta.get('reason', ''),
            'reason_label': a.meta.get('reason_label', ''),
            'title': a.meta.get('title', ''),
            'season': a.meta.get('season'),
            'meta': a.meta,
            'files': [str(f) for f in a.files],
            'file_guard': guard,
        })
    stats = {
        'loc': sum(1 for a in acts if a.kind == 'loc'),
        'shr': sum(1 for a in acts if a.kind == 'shr'),
        'keep': sum(1 for a in acts if a.kind == 'keep'),
        'exempt': sum(1 for a in acts if a.kind == 'exempt'),
    }
    rule_snapshot = _current_rule_snapshot()
    payload = {
        'schema_version': 2,
        'id': pid,
        'ts': ts,
        'rule_sig': rule_snapshot['sig'],
        'state': 'pending',
        'stats': stats,
        'actions': actions_payload,
        'guard_version': _plan_guard.VERSION,
        'file_snapshots': snapshots,
        'executed_at': None,
        'executed_result': None,
    }
    _tasks.begin_publication()
    _state.save(_eng().STATE_DIR / f'plan_{pid}.json', payload)
    return pid

def load_plan(plan_id):
    safe = re.sub(r'[^0-9a-f]', '', str(plan_id or ''))
    if not safe:
        return None
    try:
        return _state.read(_eng().STATE_DIR / ('plan_' + safe + '.json'), None)
    except (OSError, ValueError):
        return None

def save_plan_state(plan_id, state, extra=None):
    data = load_plan(plan_id)
    if not data:
        return False
    data['state'] = state
    if extra:
        data.update(extra)
    try:
        _state.save(_eng().STATE_DIR / ('plan_' + data['id'] + '.json'), data)
        return True
    except OSError:
        return False

def save_latest_scan(plan_id, result):
    ts = time.time()
    meta = dict(result or {})
    meta.update(scan_ts=ts, scan_id=plan_id)
    rule_sig = _current_rule_snapshot()['sig']
    meta['rule_sig'] = rule_sig
    _state.save(_eng().GOV_LATEST_FILE, {'schema_version': 2, 'ts': ts, 'plan_id': plan_id,
                                      'rule_sig': rule_sig, 'result': meta})

def load_latest_scan():
    try:
        return _state.read(_eng().GOV_LATEST_FILE, None)
    except (OSError, ValueError):
        return None

def load_scan_status():
    """最近一次扫描尝试，与上一次成功事实分开保存。"""
    path = _eng().STATE_DIR / 'gov_scan_status.json'
    try:
        data = _state.read(path)
        if not isinstance(data, dict) or data.get('status') not in ('success', 'failed'):
            raise ValueError('无效的扫描状态')
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {'status': 'failed', 'message': '扫描状态不可读取，请重新扫描'}


def _save_scan_status(status, message=''):
    path = _eng().STATE_DIR / 'gov_scan_status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    _state.save(path, {'status': status, 'ts': time.time(), 'message': message})


def action_inter_check(args):
    # 明确的重新扫描必须读取当前磁盘事实，不复用 30 秒内的旧 Lib。
    _eng()._invalidate_lib_cache()
    try:
        acts, S_lib, L_lib = _build_plan_with_libs()
    except LibraryScanError as error:
        _tasks.begin_publication()
        _eng()._invalidate_lib_cache()
        _save_scan_status('failed', str(error))
        log.error('%s', error)
        return {'status': 'error', 'code': 'library_scan_failed', 'message': str(error)}
    identity_conflicts = _identity_conflicts(S_lib, L_lib)
    _tasks.checkpoint()
    pid = save_plan(acts)

    # 扫描完成后顺便刷新 STRM 计数缓存。
    # 计数直接取自 build_plan 刚遍历出来的 _eng().Lib（同一次遍历，数据与计划严格一致），
    # 不再为了计数把双库重新 rglob 一遍——大库/网络挂载上这是扫描耗时翻倍的元凶。
    try:
        _eng()._strm_count_cache.update({'ts': time.time(), 'local': L_lib.strm_count, 'share': S_lib.strm_count})
        _eng()._save_strm_count_disk(L_lib.strm_count, S_lib.strm_count)
        log.info('扫描后刷新 STRM 计数: local=%d share=%d', L_lib.strm_count, S_lib.strm_count)
    except Exception as e:
        log.warning('刷新 STRM 计数缓存失败: %s', e)
    # 扫描后失效 _eng().Lib 缓存。执行阶段不再重新 build_plan：
    # 治理单已经经过 15 分钟入库静默期并由用户确认，执行只消费这份已确认快照。
    # 这样避免点击「执行清理」时再次全量遍历双库。
    _eng()._invalidate_lib_cache()
    loc = [a for a in acts if a.kind == 'loc']
    shr = [a for a in acts if a.kind == 'shr']
    keep = [a for a in acts if a.kind == 'keep']
    exempted = [a for a in acts if a.kind == 'exempt']
    write_audit_log('库间查重巡检',
                    f'扫描完成：待清理本地 {len(loc)} 项, 待清理分享 {len(shr)} 项, 保护 {len(keep)} 项, 豁免 {len(exempted)} 项',
                    extra={'plan_id': pid or ''})
    if (loc or shr) and not getattr(args, 'silent', False):
        _eng().notify_telegram(_eng().fmt_scan_text('🔍', '双库扫描完成', len(loc), len(shr), len(keep), len(exempted),
                                      len(_group_exempt_acts(exempted))))
    def _file_count(items):
        return sum(len(a.files) for a in items)
    reason_counts = defaultdict(int)
    for a in acts:
        reason = a.meta.get('reason') or ('protected' if a.kind == 'keep' else 'exempt' if a.kind == 'exempt' else 'unknown')
        reason_counts[reason] += 1
    result = {
        'status': 'success', 'plan_id': pid,
        'total_clean_cnt': len(loc) + len(shr),
        'del_local_cnt': len(loc), 'del_share_cnt': len(shr),
        'del_local_files': _file_count(loc), 'del_share_files': _file_count(shr),
        'protected_files': _file_count(keep), 'exempted_files': _file_count(exempted),
        'del_local_items': [_act_to_dict(a) for a in loc],
        'del_share_items': [_act_to_dict(a) for a in shr],
        'protected_items': [_act_to_dict(a) for a in keep],
        'exempted_items':  _group_exempt_acts(exempted),
        'exempted_count':  len(exempted),
        'quiet_skipped':   _eng()._QUIET_LAST['n'],
        'reason_counts': dict(reason_counts),
        'strm_counts': {'local': L_lib.strm_count, 'share': S_lib.strm_count,
                        'total': L_lib.strm_count + S_lib.strm_count},
        'identity_conflicts': identity_conflicts,
        'identity_conflict_count': len(identity_conflicts),
    }
    save_latest_scan(pid, result)
    _save_scan_status('success')
    return result

def action_inter_clean(args):
    # 统一互斥锁：不管从 CLI、Web 任务队列还是 Telegram Bot 线程发起，
    # 只要是真正会修改文件的清理（非 dry-run），都必须先拿到这把跨入口的文件锁，
    # 避免三个入口各自维护自己的锁导致两个清理任务同时跑。
    if args.dry_run:
        return _action_inter_clean_locked(args)
    try:
        with _eng().mutation_lock():
            return _action_inter_clean_locked(args)
    except _eng().MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}

def _action_inter_clean_locked(args):
    try:
        return _run_inter_clean(args)
    except Exception as e:
        # 执行中途异常：计划标记为 failed，不能停在 executing（网页会一直显示"执行中"）
        if args.plan and not args.dry_run:
            save_plan_state(args.plan, 'failed', {'executed_at': time.time(),
                                                  'executed_result': {'error': f'{type(e).__name__}: {e}'}})
        raise

def _confirmed_files(cur, old_act):
    """兼容旧调用方的文件收敛辅助；治理单执行本身不再调用它。"""
    planned = set(old_act.get('files') or [])
    keep = [f for f in cur.files if str(f) in planned]
    return keep, len(cur.files) - len(keep)


def _plan_action_to_act(old_act):
    """把已保存的治理单动作还原为执行对象。

    执行阶段只消费用户已经确认的治理单，不重新 build_plan。
    文件列表就是扫描+静默期过滤后的确认快照；新文件不会被带入本次执行。
    """
    kind = str(old_act.get('kind') or '')
    if kind not in ('loc', 'shr'):
        return None
    files = []
    root = _eng().L_ROOT if kind == 'loc' else _eng().S_ROOT
    for raw in old_act.get('files') or []:
        try:
            p = Path(str(raw))
            # 计划文件可能来自历史运行环境；只允许删除仍位于对应库根下的路径。
            p.relative_to(root)
        except (TypeError, ValueError, OSError):
            continue
        # 文件在扫描后消失：跳过即可；绝不为了确认它而重新扫描整个库。
        if p.exists() and p.is_file():
            files.append(p)
    if not files:
        return None
    meta = dict(old_act.get('meta') or {})
    meta.setdefault('reason', old_act.get('reason', ''))
    meta.setdefault('reason_label', old_act.get('reason_label', ''))
    meta.setdefault('title', old_act.get('title', ''))
    if old_act.get('season') is not None:
        meta.setdefault('season', old_act.get('season'))
    text = str(old_act.get('text') or '').strip()
    detail = str(old_act.get('detail') or '').strip()

    # 兼容旧版治理计划：旧计划可能只有 text，没有 detail。
    # 执行日志不能因此产生空明细。
    if not detail:
        detail = text

    return Act(kind, text, detail, files, meta)


def _run_inter_clean(args):
    skipped_details = []
    unconfirmed = 0
    validated_actions = {}
    if args.plan:
        scan_status = load_scan_status()
        if scan_status.get('status') == 'failed':
            return {'status': 'error', 'code': 'library_scan_failed',
                    'message': '最近一次媒体库扫描失败，清理已暂停，请检查目录后重新扫描成功再执行'}
        plan = load_plan(args.plan)
        if plan is None:
            return {'status': 'error', 'code': 'plan_not_found',
                    'message': '清理计划不存在/已损坏（可能是旧格式），请重新诊断'}
        st = plan.get('state')
        current_rule_sig = _current_rule_snapshot()['sig']
        plan_rule_sig = str(plan.get('rule_sig') or '')
        if plan_rule_sig and plan_rule_sig != current_rule_sig:
            if not args.dry_run:
                save_plan_state(args.plan, 'stale', {
                    'stale_at': time.time(),
                    'stale_reason': f'规则指纹变化: {plan_rule_sig} → {current_rule_sig}',
                })
            return {'status': 'error', 'code': 'plan_rule_changed',
                    'message': '清理计划生成后规则已变化，请重新扫描生成新计划'}
        if st in ('done', 'failed', 'stale'):
            return {'status': 'error', 'code': 'plan_used',
                    'message': '清理计划已执行过（state=%s），不能重复执行' % st}
        if st == 'expired':
            return {'status': 'error', 'code': 'plan_expired',
                    'message': '清理计划已过期，请重新诊断'}
        if time.time() - plan.get('ts', 0) > _eng().PLAN_TTL:
            if not args.dry_run:
                save_plan_state(args.plan, 'expired')
            return {'status': 'error', 'code': 'plan_expired',
                    'message': '清理计划已超过 2 小时未执行，已过期，请重新诊断'}
        if plan.get('guard_version') != _plan_guard.VERSION or not isinstance(plan.get('file_snapshots'), dict):
            if not args.dry_run:
                save_plan_state(args.plan, 'stale', {'stale_reason': '缺少文件校验快照，请重新扫描'})
            return {'status': 'error', 'code': 'plan_snapshot_missing',
                    'message': '旧清理计划缺少文件校验快照，请重新扫描生成新计划'}
        if not args.dry_run:
            save_plan_state(args.plan, 'executing')

        # 重要：执行治理单不再重新 build_plan。
        # 扫描阶段已经执行 15 分钟入库静默过滤，随后生成治理单并由用户确认。
        # 这里直接执行治理单保存的 files 快照：
        #   - 扫描后新出现的文件不会进入本次执行；
        #   - 扫描后已经消失的文件自然跳过；
        #   - 不再为了二次校验全量遍历双库，避免执行阶段再耗时一轮。
        old_actions = {act.get('action_id', ''): act
                       for act in plan.get('actions', []) if act.get('action_id')}
        todo = []
        for aid, old_act in old_actions.items():
            reason = _plan_guard.validate(old_act, plan['file_snapshots'], (_eng().L_ROOT, _eng().S_ROOT))
            if reason:
                skipped_details.append('%s → %s' % (old_act.get('text', '?'), reason))
                continue
            cur = _plan_action_to_act(old_act)
            if cur is None:
                skipped_details.append(
                    '%s → 计划中的待删文件已不存在或路径不在对应片库' % old_act.get('text', '?'))
                continue
            todo.append(cur)
            validated_actions[cur.action_id] = old_act
        skipped = len(old_actions) - len(todo)
    else:
        todo = [a for a in _eng().build_plan() if a.kind not in ('keep', 'exempt')]
        snapshots, guards = _execution_snapshots(todo)
        plan = {'file_snapshots': snapshots}
        validated_actions = {a.action_id: {'files': [str(f) for f in a.files], 'file_guard': guard}
                             for a, guard in zip(todo, guards)}
        skipped = 0

    n_loc = n_sh = 0
    detail, warns, audit_items = [], [], []
    for a in todo:
        # 逐动作进入删除链前再核对，捕获前面动作/外部入库期间发生的变化。
        reason = _plan_guard.validate(validated_actions[a.action_id], plan['file_snapshots'],
                                      (_eng().L_ROOT, _eng().S_ROOT))
        if reason:
            skipped += 1
            skipped_details.append('%s → %s' % (a.text, reason))
            continue
        is_loc = a.kind == 'loc'
        r = _eng().safe_delete_files(a.files, _eng().L_ROOT if is_loc else _eng().S_ROOT, _eng().CLOUD_L_ROOT if is_loc else None, args.dry_run)
        line = ('[DRY-RUN] ' if args.dry_run else '') + a.detail
        if r['errors']:
            warns.append(f'{a.text}: ' + '; '.join(r['errors'][:2])); line += ' ⚠️ 部分失败'
        if len(audit_items) < AUDIT_ITEM_CAP:
            audit_items.append(_audit_item(a, {
                'status': ('noop' if r['strm_removed'] == 0 else 'partial' if r['errors'] else 'ok'),
                'removed': r['strm_removed'], 'planned': len(a.files),
                'cloud_removed': r.get('cloud_removed', 0),
                'sidecars_removed': r.get('sidecars_removed', 0),
                'cloud_sidecars_removed': r.get('cloud_sidecars_removed', 0),
                'cloud_missing': r.get('cloud_missing', 0),
                'cloud_ambiguous': r.get('cloud_ambiguous', 0),
                'cloud_fallback': r.get('cloud_fallback', 0),
                'errors': [str(e) for e in r['errors'][:3]],
            }))
        if r['strm_removed'] == 0 and not args.dry_run:
            detail.append(line + ' (未产生变更)'); continue
        if is_loc:
            _bits = []
            if r.get('cloud_missing'):   _bits.append(f'{r["cloud_missing"]} 个未找到源')
            if r.get('cloud_ambiguous'): _bits.append(f'{r["cloud_ambiguous"]} 个多候选')
            if r.get('cloud_fallback'):  _bits.append(f'{r["cloud_fallback"]} 个仅兜底')
            if _bits:
                line += ' ⚠️ ' + '、'.join(_bits)
        line = line.strip()
        if line:
            detail.append(line)
        n_loc += is_loc; n_sh += not is_loc

    refreshed = False
    if (n_loc or n_sh) and not args.dry_run:
        refreshed = _eng().notify_emby_refresh()
        # 文件已变动，主动失效 _eng().Lib 缓存，避免下次 build_plan 用到陈旧数据
        _eng()._invalidate_lib_cache()
        # 同时失效统计缓存（总览页下次重算）
        _eng()._strm_count_cache['ts'] = 0
        _eng()._lib_stats_cache['ts'] = 0
        _eng()._lib_stats_cache['data'] = None
    if skipped:
        detail.append(f'├─ ⏭ 有 {skipped} 项文件或保留依据已变化/不可验证，已跳过，请重新扫描')
        for s in skipped_details[:5]:
            detail.append(f'│   · {s}')
    if unconfirmed:
        detail.append(f'├─ ⏭ 有 {unconfirmed} 个文件未在已确认治理单快照中，本次未删除')
    if not args.dry_run:
        _eng().purge_old()
        audit_recorded = write_audit_log('执行跨库清理',
                        f'释放本地 {n_loc} 项, 淘汰分享 {n_sh} 项 (Emby刷新: {refreshed})',
                        detail + [f'⚠️ {w}' for w in warns],
                        extra={'plan_id': args.plan or '', 'items': audit_items,
                               'n_loc': n_loc, 'n_shr': n_sh, 'refreshed': refreshed,
                               'skipped': skipped, 'warnings': len(warns),
                               'items_truncated': len(todo) > len(audit_items)})
        # Bot 端发起的清理会传 notify=False：由 Bot 自己编辑确认消息，避免重复弹两条
        if (n_loc or n_sh) and getattr(args, 'notify', True):
            _eng().notify_telegram('\n'.join([
                _eng().tg_title('🗑️', '清理完成', _eng().tg_stamp()), '',
                _eng().tg_row('💾', '释放本地', n_loc),
                _eng().tg_row('📤', '淘汰分享', n_sh),
                _eng().tg_row('🔄', 'Emby 刷新', '✅' if refreshed else '❌')]))
        if args.plan:
            plan_saved = save_plan_state(args.plan, 'done', {
                'executed_at': time.time(),
                'executed_result': {
                    'loc': n_loc, 'shr': n_sh,
                    'refreshed': refreshed,
                    'skipped': skipped,
                    'warnings': warns,
                },
            })
    if not args.dry_run and (not audit_recorded or (args.plan and not plan_saved)):
        warns.append('清理已执行，但 SQLite 存档保存失败，请查看日志中心和存储状态')
    return {'status': 'success', 'loc_cnt': n_loc, 'sh_cnt': n_sh, 'refreshed': refreshed,
            'detail': detail, 'warnings': warns, 'dry_run': args.dry_run,
            'skipped': skipped, 'unconfirmed_files': unconfirmed}
