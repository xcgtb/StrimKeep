import asyncio
import hashlib
import json

import pytest

from strimkeep import share_service
from test_phase2_share_pipeline import share_setup


SECRET = {'share_code':'code','receive_code':''}
VIDEO = {'id':'301','name':'Test.Movie.2024.mkv','size':5000,'path':'Work (2024)/'}


def test_missing_output_recovers_only_when_user_explicitly_resubmits(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, SECRET, [VIDEO])['created'][0]
        source = engine.root / entry['strm_path']
        source.unlink()
        assert not share_service.generate(store, engine.root, SECRET, [VIDEO], origin='monitor')['created']
        assert not source.exists()
        restored = share_service.generate(store, engine.root, SECRET, [VIDEO], origin='web', allow_restore=True)
        assert len(restored['created']) == 1
        assert restored['created'][0]['id'] == entry['id']
        row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (entry['id'],)).fetchone()
        assert (engine.root / row['strm_path']).is_file()
        assert '/play115/share/' + entry['id'] in (engine.root / row['strm_path']).read_text()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_never_restore_changed_existing_or_revoked_entry(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, SECRET, [VIDEO])['created'][0]
        source = engine.root / entry['strm_path']
        source.write_text('user changed this file')
        result = share_service.generate(store, engine.root, SECRET, [VIDEO], allow_restore=True)
        assert not result['created'] and source.read_text() == 'user changed this file'
        source.unlink()
        for changed in ({**VIDEO,'name':'Different.mkv'}, {**VIDEO,'size':5001}):
            assert not share_service.generate(store, engine.root, SECRET, [changed], allow_restore=True)['created']
        with store.db:
            store.db.execute('INSERT INTO share_revocations(share_code,file_id,reason,created) VALUES(?,?,?,?)', ('code','301','user',0))
        assert not share_service.generate(store, engine.root, SECRET, [VIDEO], allow_restore=True)['created']
        assert not source.exists()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_restoration_rejects_unrecoverable_legacy_token_and_symlink(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, SECRET, [VIDEO])['created'][0]
        source = engine.root / entry['strm_path']
        source.unlink()
        with store.db:
            store.db.execute('UPDATE share_strms SET token_hash=? WHERE id=?', (hashlib.sha256(b'private').hexdigest(), entry['id']))
        assert not share_service.generate(store, engine.root, SECRET, [VIDEO], allow_restore=True)['created']
        with store.db:
            store.db.execute('UPDATE share_strms SET token_hash=? WHERE id=?', (hashlib.sha256(b'').hexdigest(), entry['id']))
        source.symlink_to(tmp_path / 'missing-target')
        assert not share_service.generate(store, engine.root, SECRET, [VIDEO], allow_restore=True)['created']
        assert source.is_symlink()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_tg_resume_before_manifest_does_not_restore(tmp_path, monkeypatch):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        job_id = share_service.enqueue_organize(engine, origin='telegram', secret=SECRET)
        flags = []
        async def list_videos(*args): return [VIDEO]
        def interrupted(*args, **kwargs):
            flags.append(kwargs['allow_restore'])
            raise RuntimeError('interrupted after starting')
        monkeypatch.setattr(share_service, 'list_videos', list_videos)
        monkeypatch.setattr(share_service, 'generate', interrupted)
        for _ in range(2):
            config = json.loads(engine.job(job_id)['config'])
            with pytest.raises(RuntimeError, match='interrupted'):
                await share_service.run_organize(engine, job_id, config)
        assert flags == [True, False]
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_explicit_restore_never_overwrites_other_pending_file(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, SECRET, [VIDEO])['created'][0]
        pending_path = engine.root / entry['strm_path']
        pending_path.unlink()
        with store.db:
            store.db.execute('UPDATE share_strms SET strm_path=? WHERE id=?', ('分享影视库/missing.strm', entry['id']))
        pending_path.write_text('unrelated user content')
        result = share_service.generate(store, engine.root, SECRET, [VIDEO], origin='web', allow_restore=True)
        assert len(result['created']) == 1 and result['created'][0]['id'] == entry['id']
        assert pending_path.read_text() == 'unrelated user content'
        restored_path = engine.root / result['created'][0]['strm_path']
        assert restored_path != pending_path and restored_path.is_file()
        await http.aclose(); store.close()
    asyncio.run(scenario())
