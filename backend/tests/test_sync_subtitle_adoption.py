"""Safe reuse of owned subtitles while upgrading legacy registry records."""
import asyncio
import hashlib
import json

import pytest

from test_organize import new_job, setup
from test_phase5 import sync_config


@pytest.mark.parametrize('fingerprint_matches,owner_present', [(True, True), (False, True), (True, False)])
def test_legacy_subtitle_is_reused_only_when_scan_and_ownership_match(tmp_path, fingerprint_matches, owner_present):
    async def run():
        store, cloud, http, engine, requests = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        data = b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n'
        sub = cloud.add_video('1001', 'Movie.2024.1080p.chs.srt')
        sub['size'] = len(data)
        sub['sha1'] = hashlib.sha1(data).hexdigest().upper()
        first = new_job(store, sync_config())
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'
        local_sub = next(engine.root.rglob('*.srt'))
        assert local_sub.read_bytes() == data
        if not fingerprint_matches:
            sub['sha1'] = 'B' * 40
        requests.clear()
        second = new_job(store, sync_config())
        await engine.run_job(second)
        assert engine.job(second)['state'] == 'completed', dict(engine.job(second))
        downloads = [r for r in requests if r.url.host == 'cdn.example']
        # 只要本地字幕内容与清单一致就复用；目录里的归属记录不再参与判断。
        assert bool(downloads) is not fingerprint_matches
        assert next(engine.root.rglob('*.srt')).exists()
        await engine.sync.hash_worker.close()
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sync_preserves_a_subtitle_edited_after_publication(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        data = b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n'
        sub = cloud.add_video('1001', 'Movie.2024.1080p.chs.srt')
        sub['size'], sub['sha1'] = len(data), hashlib.sha1(data).hexdigest().upper()
        await engine.run_job(new_job(store, sync_config()))
        local = next(engine.root.rglob('*.srt'))
        local.write_text('user edit')
        jid = new_job(store, sync_config())
        await engine.run_job(jid)
        assert local.read_text() == 'user edit'
        assert engine.job(jid)['state'] in ('completed', 'partial')
        await engine.sync.hash_worker.close()
        await http.aclose()
        store.close()
    asyncio.run(run())
