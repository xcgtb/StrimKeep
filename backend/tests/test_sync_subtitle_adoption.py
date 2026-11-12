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
        old = engine.outputs.get('1', video['id'])
        source = json.loads(old['source'])
        assert source['subtitles']
        source['subtitles'] = []
        with store.db:
            store.db.execute('UPDATE output_registry SET source=? WHERE account_id=? AND file_id=?',
                             (json.dumps(source), '1', video['id']))
        if not owner_present:
            with store.db:
                store.db.execute("DELETE FROM owned_outputs WHERE file_id=? AND relative_path LIKE '%.srt'", (video['id'],))
        if not fingerprint_matches:
            sub['sha1'] = 'B' * 40
        requests.clear()
        second = new_job(store, sync_config())
        await engine.run_job(second)
        assert engine.job(second)['state'] == 'completed', dict(engine.job(second))
        downloads = [r for r in requests if r.url.host == 'cdn.example']
        assert bool(downloads) is not (fingerprint_matches and owner_present)
        assert json.loads(engine.outputs.get('1', video['id'])['source'])['subtitles'][0]['sha1'] == sub['sha1']
        await engine.sync.hash_worker.close()
        await http.aclose()
        store.close()
    asyncio.run(run())
