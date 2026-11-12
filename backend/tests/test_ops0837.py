"""0.8.37：本地巡检 / 退役输出 / 孤儿清理 也要联动清专属侧车与空目录。"""
import asyncio
import hashlib
import json
import pathlib

import pytest

from test_organize import new_job, setup
from test_phase5 import sync_config


def scheduled(**over):
    config = dict(sync_config())
    config.update({'scheduled': True})
    config.update(over)
    return config


async def run_local_sweep(engine, job):
    engine.update_job(job, state='running')
    config = json.loads(engine.job(job)['config'])
    return await engine.sync.sweep(job, config)


def test_sweep_revoked_output_removes_its_sidecars(tmp_path):
    """已撤销输出的 STRM 与同名 nfo / mediainfo 一起清掉。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        await engine.run_job(new_job(store, sync_config()))
        registered = engine.outputs.get('1', '1000')
        strm = engine.root / registered['relative_path']
        nfo = strm.with_suffix('.nfo')
        info = strm.with_name(strm.stem + '-media-info.json')
        nfo.write_text('<movie/>', encoding='utf-8')
        info.write_text('{}', encoding='utf-8')

        with store.db:
            store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?', (registered['playback_id'],))
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        row = engine.job(job)
        assert not strm.exists(), '已撤销的 STRM 应被清掉'
        assert not nfo.exists() and not info.exists(), '同名侧车也要一起清掉'
        assert '清理已撤销输出' in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_orphan_strm_removes_its_sidecars(tmp_path):
    """巡检清理播不了的 STRM 时，同名 nfo 一起清掉。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        await engine.run_job(new_job(store, sync_config()))
        root = engine.root / 'stray'
        root.mkdir(parents=True, exist_ok=True)
        dead = root / 'dead.strm'
        dead.write_text('https://play.example/play/deadbeefdeadbeef/x', encoding='utf-8')
        side = root / 'dead.nfo'
        side.write_text('n', encoding='utf-8')

        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        assert not dead.exists() and not side.exists(), 'STRM 与同名侧车都要清掉'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_retire_removes_sidecars_with_the_strm(tmp_path):
    """退役受管输出时，同目录的 nfo 不再挡住清理。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        job = new_job(store, sync_config())
        engine.update_job(job, state='running')      # retire 在 boundary 里校验任务仍在运行
        relative = '媒体库/电影/片 (2020)/片 (2020).strm'
        path = engine.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b'http://play/1'
        path.write_bytes(content)
        nfo = path.with_suffix('.nfo')
        nfo.write_text('n', encoding='utf-8')

        warnings = engine.outputs.retire(job, '1', '1000',
                                         {relative: hashlib.sha256(content).hexdigest()})
        assert warnings == []
        assert not path.exists() and not nfo.exists()
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_unlink_recorded_purge_fails_closed_when_strm_remains(tmp_path):
    """purge 模式下删不掉 STRM 必须抛错，调用方据此保留台账。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        job = new_job(store, sync_config())
        relative = '媒体库/电影/片 (2020)/片 (2020).strm'
        path = engine.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x', encoding='utf-8')

        original = pathlib.Path.unlink

        def refuse(self, *args, **kwargs):
            raise OSError('locked')

        pathlib.Path.unlink = refuse
        try:
            try:
                engine.outputs.unlink_recorded(job, relative, path, purge=True)
            except OSError:
                pass
            else:
                raise AssertionError('删不掉 STRM 时必须抛错，不能假装完成')
        finally:
            pathlib.Path.unlink = original
        assert path.exists()
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_offline_link_limit_is_100():
    """离线下载单次上限从 20 提高到 100；超过直接 422，不静默截断。"""
    from fastapi import HTTPException
    from strimkeep.offline_api import OFFLINE_MAX_LINKS, ensure_link_count

    assert OFFLINE_MAX_LINKS == 100
    ensure_link_count(['magnet:?xt=test'] * OFFLINE_MAX_LINKS)          # 恰好 100 条允许
    with pytest.raises(HTTPException) as exc:
        ensure_link_count(['magnet:?xt=test'] * (OFFLINE_MAX_LINKS + 1))
    assert exc.value.status_code == 422 and '100' in str(exc.value.detail)
