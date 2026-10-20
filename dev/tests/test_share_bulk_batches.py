"""Bounded share batch checks; no files are actually deleted.

Uses existing repository's isolated batch fixture.
"""
import time
import pytest

@pytest.fixture(autouse=True)
def unbound_batch_sources(ctx, monkeypatch):
    # These batch-size cases stub _collect with synthetic paths; source binding
    # and filesystem ownership are covered by the real-root integration suite.
    monkeypatch.setattr(ctx[0].cloud_bridge, "snapshots", lambda *args: {})


from dev.tests.test_batch_library_delete import ctx


def test_328_share_titles_single_preview_and_six_batches(ctx, monkeypatch):
    mod, engine, _series, _episodes, _rev, accepted, pager = ctx
    rows = [dict(id=f's{i}', series_ids=[f's{i}'], name=f'样例{i}',
                 year=2026, tmdb_id=str(10000+i), in_share=True, in_local=False)
            for i in range(328)]
    pager._filtered = lambda *args: rows
    mod._collect = lambda row: (['/verified/'+row['id']+'.strm'], row['id'])

    job = mod.start_preview({'target':'share','scope':'share','selection':'all'})
    assert job['status']=='accepted'
    for _ in range(200):
        status = mod.preview_status(job['job_id'])
        if status['status'] == 'done':
            break
        time.sleep(.01)
    assert status['status'] == 'done'
    preview = status['result']
    assert preview['status']=='success'
    assert preview['count']==328 and preview['batch_count']==6
    assert preview['strm_count']==328

    called = []
    def safe_deleter(files,target,name,media_type,series_id):
        assert target=='share'
        called.append((series_id,tuple(str(f) for f in files)))
        return {'status':'success','strm_removed':len(files)}

    first = mod.execute({'target':'share','preview_id':preview['preview_id'],
                         'confirmation':'删除分享库','confirm':True,'operation_id':'d'*32},safe_deleter)
    assert first['status']=='accepted'
    progress=[]
    result=accepted.pop()[1](progress.append)
    assert len(called)==328
    assert result['batch_total']==6 and result['failed']==0 and result['strm_removed']==328
    assert progress[0]['batch_index']==1 and progress[-1]['batch_index']==6
    assert progress[-1]['processed']==328
    assert mod.execute({'target':'share','preview_id':preview['preview_id'],
                        'confirmation':'删除分享库','confirm':True,'operation_id':'e'*32},safe_deleter)['status']=='error'


def test_share_batch_respects_file_count_and_local_limit(ctx):
    mod, *_ = ctx
    small = [{'count': 180, 'id': str(i)} for i in range(110)]
    groups = mod._batches(small,'share')
    assert sum(map(len, groups)) == 110
    assert all(len(group)<=60 and sum(row['count'] for row in group)<=3000 for group in groups)
    import pytest
    with pytest.raises(ValueError):
        mod._batches([{'count':3001,'id':'x'}],'share')
    assert len(mod._batches(small[:20],'local'))==1


def test_share_600_titles_but_local_still_20(ctx, monkeypatch):
    mod, _engine, _series, _episodes, _rev, _accepted, pager = ctx
    pager._filtered = lambda *_: [
        dict(id=f's{i}', series_ids=[f's{i}'], name=f'剧集{i}', year=2026,
             tmdb_id=str(10000+i), in_share=True, in_local=True)
        for i in range(600)
    ]
    mod._collect = lambda row: ([f'/verified/{row["id"]}.strm'], row['id'])
    preview = mod.preview({'target':'share', 'scope':'share', 'selection':'all'})
    assert preview['status'] == 'success'
    assert preview['count'] == 600 and preview['batch_count'] == 10
    assert preview['strm_count'] == 600
    local = mod.preview({'target':'local', 'scope':'local', 'selection':'all'})
    assert local['status'] == 'error' and '20' in local['message']


def test_total_18000_strm_limit_blocks_operation_before_delete(ctx):
    mod, _engine, _series, _episodes, _rev, accepted, pager = ctx
    pager._filtered = lambda *_: [
        dict(id=f's{i}', series_ids=[f's{i}'], name=f'作品{i}', year=2026,
             tmdb_id=str(20000+i), in_share=True, in_local=False)
        for i in range(7)
    ]
    mod._collect = lambda row: ([f'/verified/{row["id"]}/{i}.strm' for i in range(3000)], row['id'])
    result = mod.preview({'target':'share','scope':'share','selection':'all'})
    assert result['status'] == 'error' and '总量安全上限' in result['message']
    assert not accepted
