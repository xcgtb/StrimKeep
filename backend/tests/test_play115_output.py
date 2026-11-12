"""play115：同款 STRM 身份（身份进 STRM，取链不查库）。"""
from strimkeep.playaddr import identity_of, play115_candidates, play115_url

BASE = 'http://192.0.2.10:9000'
SOURCE = {
    'id': '3547116211433113327',
    'name': '小城良方.2026.S01E02.第2集.2160p.WEB-DL.H.265.8-bit.25fps.DDP 5.1.mkv',
    'pickcode': 'e5pq6vbkyyacezf32',
    'sha1': '6B8426504E6C59D5EE3FDCEDB900D6F55235F42B',
    'size': 15177603331,
    'parent_id': '3547117112973592055',
}


def test_identity_requires_all_four_fields():
    assert identity_of(SOURCE) == ('e5pq6vbkyyacezf32', '3547116211433113327',
                                   '6B8426504E6C59D5EE3FDCEDB900D6F55235F42B', 15177603331)
    for missing in ('pickcode', 'id', 'sha1', 'size'):
        broken = {k: v for k, v in SOURCE.items() if k != missing}
        assert identity_of(broken) is None, missing
    assert identity_of({}) is None
    assert identity_of(None) is None
    # size=0 是合法值，不能被当成缺失
    assert identity_of({**SOURCE, 'size': 0}) is not None


def test_url_shape_and_encoding():
    url = play115_url(BASE + '/', 'pc', 'fid', 'SHA1', 12, 'a b/测试.mkv')
    # 名称段整体编码：空格不能裸奔，斜杠不能当成路径分隔
    assert url == BASE + '/play115/pc/fid/SHA1/12/a%20b%2F%E6%B5%8B%E8%AF%95.mkv'
    assert ' ' not in url


def test_candidates_cover_rename():
    forms = play115_candidates(BASE, SOURCE)
    # 规范形态（无结尾换行）在前，历史形态（带换行）在后。
    assert len(forms) == 2
    assert not forms[0].endswith(b'\n') and forms[1].endswith(b'\n')
    assert forms[0].decode().startswith(BASE + '/play115/e5pq6vbkyyacezf32/3547116211433113327/')

    # 改名后旧 STRM 仍按旧名称：两种名称都必须认作「我们的输出」。
    renamed = play115_candidates(BASE, SOURCE, '小城良方.2026.S01E02.第2集.mkv')
    assert len(renamed) == 4
    # 台账里的名称始终算我们的输出；改名后的新名称也要认。
    assert forms[0] in renamed
    assert any('%E7%AC%AC2%E9%9B%86.mkv' in f.decode() for f in renamed)


def test_candidates_empty_without_identity():
    assert play115_candidates(BASE, {'id': 'x', 'name': 'n'}) == []


def test_identity_prefers_listing_over_original():
    """整理侧 actual 优先：改名后 actual 才是云端真实名称。"""
    from urllib.parse import quote
    actual = {**SOURCE, 'name': '新名字.mkv'}
    assert identity_of(actual)[0] == identity_of(SOURCE)[0]
    # 名称段是百分号编码的，比对编码后的结果
    assert quote('新名字.mkv', safe='') in play115_candidates(BASE, actual)[0].decode()


def test_second_sync_keeps_play115_output_untouched(tmp_path):
    """未变化的输出不能被当成「变了」重写：否则每次同步都改写一遍 STRM，
    还会连带触发 Emby 通知（0.7.12 的 unchanged() 曾漏认 play115）。"""
    import asyncio

    from test_organize import new_job, setup
    from test_phase5 import sync_config

    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='Movie.2024.1080p.mkv')['sha1'] = 'A' * 40

        first = new_job(store, sync_config())
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed', dict(engine.job(first))

        registered = engine.outputs.get('1', '1000')
        path = engine.root / registered['relative_path']
        assert path.read_text().startswith('https://play.example/play115/')
        before = (path.read_bytes(), path.stat().st_mtime_ns)

        second = new_job(store, sync_config())
        await engine.run_job(second)
        assert engine.job(second)['state'] == 'completed', dict(engine.job(second))
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before, '未变化的输出不该被重写'

        await http.aclose()
        store.close()

    asyncio.run(run())
