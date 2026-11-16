"""已进「分享STRM未识别」的文件不该被频道重扫重新生成。

现场（2026-11-16）：分享整理失败进未识别目录后，频道每 5 分钟重读同一条消息，
`generate()` 按 revoked=0 查不到旧记录，就又建一条 STRM → 新整理任务 → 再未识别，
一轮 6 条，任务号 1739/1740/1741……一直涨。
"""
import asyncio

from strimkeep import share_service
from test_phase2_share_pipeline import share_setup


def test_unidentified_share_strm_is_not_regenerated(tmp_path):
    store, cloud, http, engine, _ = share_setup(tmp_path)
    secret = {'share_code': 'loop-share', 'receive_code': ''}
    videos = [{'id': '7', 'name': '以爱相拥.BLANK The Series.EP01.1080P.mp4',
               'size': 1024, 'path': ''}]
    first = share_service.generate(store, engine.root, secret, videos)
    assert len(first['created']) == 1
    row = store.db.execute('SELECT id,strm_path FROM share_strms WHERE share_code=?',
                           ('loop-share',)).fetchone()

    # 模拟识别失败后的处置：STRM 移进未识别目录，登记改路径并撤销入口。
    moved = (share_service.UNIDENT_NAME
             + '/以爱相拥 BLANK The Series/以爱相拥.EP01.strm')
    target = engine.root / moved
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('http://host/play115/share/' + row['id'] + '/x', encoding='utf-8')
    (engine.root / row['strm_path']).unlink()
    with store.db:
        store.db.execute('UPDATE share_strms SET strm_path=?,revoked=1 WHERE id=?', (moved, row['id']))

    again = share_service.generate(store, engine.root, secret, videos)
    assert again['created'] == []
    assert any('未识别' in item['reason'] for item in again['skipped'])
    assert store.db.execute('SELECT count(*) FROM share_strms').fetchone()[0] == 1
    asyncio.run(http.aclose())
    store.close()
