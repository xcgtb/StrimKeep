"""0.9.50 回归：账本体检只读、只报，不改任何数据。"""
import os
import time
from pathlib import Path

from strimkeep.ledger_audit import audit, orphans
from strimkeep.metrics import Metrics
from strimkeep.store import Store

PASSWORD = 'test-password-1234'


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


def _share_row(store, sid, path):
    with store.db:
        store.db.execute(
            'INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) '
            'VALUES(?,?,?,?,?,?,?)', (sid, 'code', 'fid-' + sid, sid, path, 'h', 0))


def test_audit_counts_stale_unregistered_and_orphans(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    for name in ('分享影视库', '分享STRM未识别'):
        (root / name).mkdir(parents=True)
    # 正常：STRM 有登记，且有同名 nfo / 字幕 / media-info
    (root / '分享影视库/a.strm').write_text('http://x/play115/share/s1', encoding='utf-8')
    (root / '分享影视库/a.nfo').write_text('nfo', encoding='utf-8')
    (root / '分享影视库/a.zh.srt').write_text('srt', encoding='utf-8')
    (root / '分享影视库/a-media-info.json').write_text('{}', encoding='utf-8')
    # 目录级侧车：属于整部剧，不算孤儿
    (root / '分享影视库/tvshow.nfo').write_text('nfo', encoding='utf-8')
    # 孤儿侧车：没有同名 STRM
    (root / '分享影视库/orphan.nfo').write_text('nfo', encoding='utf-8')
    # 未登记 STRM
    (root / '分享影视库/b.strm').write_text('http://x/play115/share/s2', encoding='utf-8')
    # 待整理目录里的 STRM 本来就没有登记行，不能算未登记
    (root / '分享STRM未识别/c.strm').write_text('http://x/play115/share/s3', encoding='utf-8')
    _share_row(store, 's1', '分享影视库/a.strm')
    _share_row(store, 's9', '分享影视库/gone.strm')          # 文件已不在 → 悬空登记

    result = audit(store, root)
    assert result['strm'] == 3
    assert result['unregistered'] == 1                       # 只有 b.strm
    assert result['stale_active'] == 1                       # 只有 gone.strm
    assert result['orphan_sidecar'] == 1                     # 只有 orphan.nfo
    assert result['at'] > 0
    store.close()


def test_audit_sees_active_output_rows_and_media_library(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    (root / '媒体库').mkdir(parents=True)
    (root / '媒体库/ep.strm').write_text('http://x/play115/pc/fid/SHA/1/ep.mkv', encoding='utf-8')
    with store.db:
        store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,"
                         "token_hash,revoked,created) VALUES('p1','f1','pc','ep',"
                         "'媒体库/ep.strm','h',0,0)")
        store.db.execute('INSERT INTO output_registry(account_id,file_id,source,trail,'
                         'playback_id,token_cipher,relative_path,files,active,updated) '
                         "VALUES('acc','f1','{}','{}','p1','c','媒体库/ep.strm','{}',1,0)")
        store.db.execute('INSERT INTO output_registry(account_id,file_id,source,trail,'
                         'playback_id,token_cipher,relative_path,files,active,updated) '
                         "VALUES('acc','f2','{}','{}','p2','c','媒体库/old.strm','{}',1,0)")
    result = audit(store, root)
    assert result['unregistered'] == 0                       # 媒体库也算库根
    assert result['stale_active'] == 1                       # output_registry 的 old.strm
    store.close()


def test_metrics_exposes_ledger_drift(tmp_path):
    store = make_store(tmp_path)
    store.set_meta('ledger_audit_last', {'at': 1700000000, 'strm': 10,
                                         'stale_active': 2, 'unregistered': 3,
                                         'orphan_sidecar': 4})
    app = type('App', (), {'version': '0.9.50', 'state': type('S', (), {})()})()
    body = Metrics(store).render(app)
    assert 'strimkeep_ledger_drift{kind="stale_active"} 2' in body
    assert 'strimkeep_ledger_drift{kind="unregistered"} 3' in body
    assert 'strimkeep_ledger_drift{kind="orphan_sidecar"} 4' in body
    assert 'strimkeep_ledger_audit_timestamp 1700000000' in body
    store.close()


def test_orphans_respect_the_window_and_ownership(tmp_path):
    """孤儿清单只列本项目生成、且过了保护窗口的侧车；数量与 audit() 一致。"""
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    (root / '分享影视库').mkdir(parents=True)
    (root / '分享影视库/a.strm').write_text('http://x/play115/share/s1', encoding='utf-8')
    (root / '分享影视库/a.nfo').write_text('<tmdbid>3</tmdbid>', encoding='utf-8')   # 有主
    old_nfo = root / '分享影视库/old - S01E01.nfo'
    old_nfo.write_text('<episodedetails><tmdbid>1</tmdbid></episodedetails>', encoding='utf-8')
    old_ass = root / '分享影视库/old - S01E02.ass'
    old_ass.write_text('[Script Info]', encoding='utf-8')
    foreign = root / '分享影视库/foreign - S01E03.nfo'
    foreign.write_text('someone else', encoding='utf-8')
    fresh = root / '分享影视库/fresh - S01E04.nfo'
    fresh.write_text('<tmdbid>2</tmdbid>', encoding='utf-8')
    old = time.time() - 30 * 86400
    for path in (old_nfo, old_ass, foreign):
        os.utime(path, (old, old))

    items = orphans(store, root, min_age=7 * 86400)
    by_name = {Path(item['relative']).name: item for item in items}
    assert set(by_name) == {'old - S01E01.nfo', 'old - S01E02.ass',
                            'foreign - S01E03.nfo', 'fresh - S01E04.nfo'}
    assert by_name['old - S01E01.nfo']['deletable'] is True
    assert by_name['old - S01E02.ass']['deletable'] is True
    assert by_name['foreign - S01E03.nfo']['deletable'] is False   # 归属不明
    assert by_name['fresh - S01E04.nfo']['deletable'] is False     # 太新
    assert len(items) == audit(store, root)['orphan_sidecar']      # 两套规则必须一致
    store.close()

