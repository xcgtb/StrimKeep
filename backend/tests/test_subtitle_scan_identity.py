"""全量同步：字幕直接用清单身份，不再逐条回查云端。

对附属文件只发一次直链请求；旧实现每个字幕会多发两次 cloud.file()。
这里锁定新契约：清单新鲜 + sha1/size/pickcode 齐全时，字幕零回查，
下载后用本地摘要比对，写出的内容必须与云端字节一致。
"""
import asyncio
import hashlib
from pathlib import Path

from test_organize import new_job, setup
from test_phase5 import sync_config

SCRIPT = b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n'


def test_fresh_scan_subtitle_needs_no_lookup(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video('1000', '剧集 (2024) S01E03.WEB-DL.mkv', '100')
        video['sha1'] = 'A' * 40
        sub = cloud.add_video('1001', Path(video['name']).stem + '.chs.srt', '100')
        sub['sha1'] = hashlib.sha1(SCRIPT).hexdigest().upper()
        sub['size'] = len(SCRIPT)
        lookups = []
        real_file = cloud.file

        async def counting(fid):
            lookups.append(fid)
            return await real_file(fid)

        cloud.file = counting
        config = sync_config(['100'])
        jid = new_job(store, config)
        engine.update_job(jid, state='running')
        await engine.sync.manifest(jid, dict(config))
        for row in store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,)).fetchall():
            await engine.sync.process(jid, row, dict(config))
        assert '1001' not in lookups, '字幕不应该再逐条回查云端'
        written = list((tmp_path / 'strm').rglob('*.srt'))
        assert written, '字幕应已落盘'
        assert written[0].read_bytes() == SCRIPT
        await http.aclose()
        store.close()

    asyncio.run(scenario())


def test_stale_or_missing_identity_still_verifies(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video('1000', '剧集 (2024) S01E04.WEB-DL.mkv', '100')
        video['sha1'] = 'A' * 40
        sub = cloud.add_video('1001', Path(video['name']).stem + '.chs.srt', '100')
        # sha1 不是 40 位十六进制：无法用清单身份，必须回云端核实
        sub['sha1'] = 'hash1001'
        lookups = []
        real_file = cloud.file

        async def counting(fid):
            lookups.append(fid)
            return await real_file(fid)

        cloud.file = counting
        config = sync_config(['100'])
        jid = new_job(store, config)
        engine.update_job(jid, state='running')
        await engine.sync.manifest(jid, dict(config))
        for row in store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,)).fetchall():
            await engine.sync.process(jid, row, dict(config))
        assert '1001' in lookups, '身份不可信时必须回云端核实'
        await http.aclose()
        store.close()

    asyncio.run(scenario())


def test_fresh_scan_metadata_needs_no_lookup(tmp_path):
    """nfo 等附属文件同样只取一次直链：清单身份可用时不再逐条回查。"""
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video('1000', '剧集 (2024) S01E05.WEB-DL.mkv', '100')
        video['sha1'] = 'A' * 40
        nfo = cloud.add_video('1002', 'tvshow.nfo', '100')
        nfo['sha1'] = hashlib.sha1(SCRIPT).hexdigest().upper()
        nfo['size'] = len(SCRIPT)
        lookups = []
        real_file = cloud.file

        async def counting(fid):
            lookups.append(fid)
            return await real_file(fid)

        cloud.file = counting
        config = {**sync_config(['100']), 'metadata_extensions': ['.nfo', '.srt', '.ass']}
        jid = new_job(store, config)
        engine.update_job(jid, state='running')
        # manifest 会把 sync_metadata 写回传入的 dict，这里必须用同一份引用
        cfg = dict(config)
        await engine.sync.manifest(jid, cfg)
        for row in store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,)).fetchall():
            await engine.sync.process(jid, row, cfg)
        assert [x['name'] for x in cfg['sync_metadata']] == ['tvshow.nfo']
        await engine.sync.download_metadata(jid, cfg)
        assert '1002' not in lookups, 'nfo 不应该再逐条回查云端'
        assert list((tmp_path / 'strm').rglob('*.nfo')), 'nfo 应已落盘'
        await http.aclose()
        store.close()

    asyncio.run(scenario())
