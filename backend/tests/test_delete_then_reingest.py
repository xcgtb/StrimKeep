"""深度删除 → 重新加入：分享侧必须是全新入库，不命中旧登记。"""
import asyncio

from strimkeep import share_service
from strimkeep.deep_delete import apply, match_targets

from test_deep_delete import FakeEngine, make_store


def test_share_delete_then_reingest_is_brand_new(tmp_path):
    async def run():
        store = make_store(tmp_path)
        engine = FakeEngine(store, tmp_path / 'strm')
        local = engine.root / '分享影视库/电影/分享电影.strm'
        local.parent.mkdir(parents=True)
        local.write_text('http://play-share', encoding='utf-8')
        targets = {'managed': [], 'share': match_targets(
            store, {'type': 'Movie', 'path': str(local)})['share']}
        assert targets['share']
        result = await apply(engine, targets, dry_run=False)
        assert result['errors'] == []
        assert not local.exists()
        assert store.db.execute('SELECT count(*) FROM share_strms').fetchone()[0] == 0

        generated = share_service.generate(
            store, engine.root, {'share_code': 'swabc', 'receive_code': 'abcd'},
            [{'id': '3002', 'name': '分享电影.mkv', 'size': 5000, 'path': 'Work/'}])
        assert len(generated['created']) == 1
        rows = list(store.db.execute('SELECT id FROM share_strms'))
        assert len(rows) == 1 and rows[0]['id'] != 's1'
        store.close()

    asyncio.run(run())
