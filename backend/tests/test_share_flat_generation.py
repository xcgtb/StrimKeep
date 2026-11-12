import asyncio
from pathlib import Path

from strimkeep import share_service
from test_phase2_share_pipeline import share_setup


def test_root_video_is_flat_while_real_share_folders_remain(tmp_path):
    store, cloud, http, engine, _ = share_setup(tmp_path)
    secret = {'share_code': 'root-share', 'receive_code': ''}
    videos = [{'id': '1', 'name': '电影.mkv', 'size': 1024, 'path': ''},
              {'id': '2', 'name': '剧集.mkv', 'size': 1024, 'path': '原始目录/'}]
    result = share_service.generate(store, engine.root, secret, videos)
    paths = {item['name']: item['strm_path'] for item in result['created']}
    assert paths['电影.mkv'] == '分享STRM待整理/电影.mkv.strm'
    assert paths['剧集.mkv'] == '分享STRM待整理/原始目录/剧集.mkv.strm'
    for item in result['created']:
        assert (engine.root / item['strm_path']).read_text().startswith('https://play.example/play115/share/' + item['id'] + '/')
    assert not share_service.generate(store, engine.root, secret, videos)['created']
    asyncio.run(http.aclose()); store.close()


def test_flat_same_names_never_overwrite_existing_or_each_other(tmp_path):
    store, cloud, http, engine, _ = share_setup(tmp_path)
    pending, _, _ = share_service.directories(engine.root)
    existing = pending / '电影.mkv.strm'
    existing.write_text('existing')
    video = {'id': '1', 'name': '电影.mkv', 'size': 1024, 'path': ''}
    first = share_service.generate(store, engine.root, {'share_code': 'first', 'receive_code': ''}, [video])['created'][0]
    second = share_service.generate(store, engine.root, {'share_code': 'second', 'receive_code': ''}, [video])['created'][0]
    assert existing.read_text() == 'existing'
    assert first['strm_path'] != second['strm_path']
    assert Path(first['strm_path']).parent == Path('分享STRM待整理')
    assert (engine.root / first['strm_path']).read_text().startswith('https://play.example/play115/share/' + first['id'] + '/')
    assert (engine.root / second['strm_path']).read_text().startswith('https://play.example/play115/share/' + second['id'] + '/')
    assert not (pending / 'first').exists() and not (pending / 'second').exists()
    asyncio.run(http.aclose()); store.close()


def test_align_existing_urls_preserves_identity_and_ignores_unowned_files(tmp_path):
    import json
    from urllib.parse import urlsplit, unquote
    store, cloud, http, engine, _ = share_setup(tmp_path)
    folder = engine.root / share_service.LIB_NAME / '剧集'; folder.mkdir(parents=True)
    good = folder / '作品.S01E01.strm'; good.write_text('http://host/play/share/good?token=legacy')
    other = folder / '作品.S01E02.strm'; other.write_text('http://host/play/share/not-owner')
    pending = engine.root / share_service.PENDING_NAME / '待整理.strm'; pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text('http://host/play/share/pending')
    for sid,path in [('good',good),('other',other),('pending',pending)]:
        with store.db:
            store.db.execute("INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created) VALUES(?,?,?,?,?,?,?,?,0,?)",
                             (sid,'x','','f'+sid,'','原文件.mkv',str(path.relative_to(engine.root)),'',1))
    assert share_service.align_existing_share_urls(store, engine.root) == 1
    parsed = urlsplit(good.read_text())
    assert unquote(parsed.path) == '/play/share/good/作品.S01E01.mkv'
    assert parsed.query == 'token=legacy'
    assert other.read_text() == 'http://host/play/share/not-owner'
    assert pending.read_text() == 'http://host/play/share/pending'
    assert json.loads(store.meta('share_url_refresh_pending')) == [str(good.relative_to(engine.root))]
    assert share_service.align_existing_share_urls(store, engine.root) == 0
    asyncio.run(http.aclose()); store.close()


def test_align_corrects_bogus_numeric_extension_from_older_release(tmp_path):
    from urllib.parse import unquote, urlsplit
    store, cloud, http, engine, _ = share_setup(tmp_path)
    folder = engine.root / share_service.LIB_NAME / '剧集'; folder.mkdir(parents=True)
    path = folder / '作品.DDP 5.1.strm'
    path.write_text('http://host/play/share/sid/%E4%BD%9C%E5%93%81.DDP%205.1.1')
    with store.db:
        store.db.execute("INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created) VALUES(?,?,?,?,?,?,?,?,0,?)",
                         ('sid','x','','f1','','作品.DDP 5.1',str(path.relative_to(engine.root)),'',1))
    assert share_service.align_existing_share_urls(store, engine.root) == 1
    assert unquote(urlsplit(path.read_text()).path) == '/play/share/sid/作品.DDP 5.1'
    asyncio.run(http.aclose()); store.close()
