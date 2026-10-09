# -*- coding: utf-8 -*-
"""执行记录的结构化明细：清理后留存逐项治理清单快照，供「执行记录」点开查看详情。

运行方式同其它测试：`pytest dev/tests/ -v`。
"""
import os
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_audit_detail_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402
import governance  # noqa: E402
import logger  # noqa: E402


def _make_share_season(n=3):
    d = _TMP / 'share' / '剧集' / '欧美剧集' / '破产姐妹 (2011) {tmdb-39340}' / 'Season 5'
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(1, n + 1):
        f = d / ('破产姐妹.2011.S05E0%d.1080p.WEB-DL.strm' % i)
        f.write_text('x')
        files.append(f)
    return files


def test_clean_keeps_item_snapshot_in_audit_record():
    files = _make_share_season()
    meta = {'reason': 'local_better', 'reason_label': '本地择优-删分享', 'title': '破产姐妹 (2011)',
            'season': 5, 'share_paths': [str(f) for f in files] * 40, 'share_count': 120,
            'match_basis_detail': '规范化剧名「破产姐妹 (2011)」 + 年份「2011」'}
    act = governance.Act('shr', '《破产姐妹 (2011)》S05 (本地更优 → 淘汰分享)', '本地更优 → 淘汰分享', files, meta)
    pid = governance.save_plan([act])
    res = engine.action_inter_clean(Namespace(plan=pid, dry_run=False, kw='', notify=False))
    assert res['status'] == 'success' and res['sh_cnt'] == 1

    rec = logger.read_recent(1)[0]
    assert rec['category'] == '执行跨库清理'
    ex = rec['extra']
    assert ex['plan_id'] == pid and ex['n_shr'] == 1
    it = ex['items'][0]
    assert it['kind'] == 'shr' and it['season'] == 5 and it['reason_label'] == '本地择优-删分享'
    assert it['result']['status'] == 'ok' and it['result']['removed'] == 3
    # 路径被截断，且打了标记
    assert len(it['meta']['share_paths']) == governance.AUDIT_PATH_CAP
    assert it['meta']['paths_truncated'] is True
    # 按 id 可取回完整快照
    assert engine.db_get_audit(rec['id'])['extra']['items'][0]['title'] == '破产姐妹 (2011)'


def test_record_without_extra_stays_compatible():
    logger.write('目录清理', '无结构化明细的旧格式', ['清理 1 个目录'])
    rec = logger.read_recent(1)[0]
    assert 'extra' not in rec and rec['details'][0] == '清理 1 个目录'


def test_old_audit_table_gets_extra_column():
    import sqlite3
    con = sqlite3.connect(':memory:')
    con.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT, ts_epoch REAL, category TEXT, "
                "title TEXT, details TEXT, rule_sig TEXT)")
    from storage import _ensure_audit_extra
    _ensure_audit_extra(con)
    assert 'extra' in {r[1] for r in con.execute('PRAGMA table_info(audit)')}
