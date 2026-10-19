"""Complete, read-only inventories, isolated by account and successful scan."""
import copy
import json
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import accounts, inventory, jobs, recognition, store
from app.cloud.errors import CloudError
from app.domain.inventory import collect, episodes


def tree(cloud):
    rows = [('10','0','电影',True), ('11','10','电影甲 (2024) [tmdb-42]',True),
            ('12','11','Movie.mkv',False), ('13','0','剧集',True),
            ('14','13','剧集乙 (2023) {tmdb-42}',True), ('15','14','Season00',True),
            ('16','15','[01].mkv',False), ('17','14','Show.S01E01-E03.mkv',False),
            ('18','14','Show.S01E04E05.mkv',False), ('19','14','extra.txt',False)]
    cloud.remote.files = {'0': copy.deepcopy(cloud.remote.files['0'])}
    for fid,parent,name,directory in rows:
        cloud.remote.files[fid] = {'id':fid,'parent_id':parent,'name':name,'is_dir':directory,'size':0 if directory else 1024}


def scan(cloud, **options):
    jid = inventory.start(cloud.id, '0', enrich=False, **options)
    jobs.execute(jid)
    return jid


def test_complete_catalogue_preserves_specials_and_source_files(cloud):
    tree(cloud)
    original = copy.deepcopy(cloud.remote.files)
    jid = scan(cloud)
    assert store.job(jid)['state'] == 'success'
    result = inventory.listing(cloud.id)
    assert result['counts'] == {'movie':1,'tv':1,'':0,'all':2}
    assert result['summary']['video_count'] == 4
    tv = next(row for row in result['items'] if row['kind'] == 'tv')
    assert tv['episodes'] == [[0,1],[1,1],[1,2],[1,3],[1,4],[1,5]]
    assert tv['episode_count'] == 6 and tv['video_count'] == 3
    assert cloud.remote.files == original and cloud.remote.writes == []
    assert inventory.listing(cloud.id,kind='movie')['total'] == 1
    assert inventory.listing(cloud.id,keyword='剧集乙')['total'] == 1
    assert inventory.listing(cloud.id,limit=1)['has_more']


def test_reads_all_more_than_500_and_caps_detail_list(cloud):
    tree(cloud)
    for i in range(601):
        fid = str(1000+i)
        cloud.remote.files[fid] = {'id':fid,'parent_id':'11','name':f'Movie.{i}.mkv','is_dir':False,'size':2}
    scan(cloud)
    movie = next(row for row in inventory.listing(cloud.id)['items'] if row['kind'] == 'movie')
    assert movie['video_count'] == 602 and movie['size'] == 1024 + 1202
    assert len(movie['files']) == 100 and movie['files_truncated']


def test_metadata_cache_separates_movie_and_tv_ids(cloud,monkeypatch):
    tree(cloud)
    tmdb = Mock()
    tmdb.get.side_effect = lambda path: {'id':42,'title':path,'poster_path':'/poster.jpg'}
    monkeypatch.setattr(recognition,'tmdb',lambda:tmdb)
    jid = inventory.start(cloud.id,'0',enrich=True)
    jobs.execute(jid)
    rows = inventory.listing(cloud.id)['items']
    assert all(row['metadata_verified'] for row in rows)
    assert {row['title'] for row in rows} == {'/movie/42','/tv/42'}
    assert tmdb.get.call_count == 2


def test_metadata_failure_never_fakes_identification(cloud,monkeypatch):
    tree(cloud)
    monkeypatch.setattr(recognition,'tmdb',Mock(side_effect=CloudError('missing key')))
    jid = inventory.start(cloud.id,'0',enrich=True)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'success'
    result = inventory.listing(cloud.id)
    assert result['summary']['metadata_failed'] == 2
    assert all(not row['metadata_verified'] and not row['poster'] for row in result['items'])


def test_changed_names_sizes_and_parents_replace_success_only(cloud):
    tree(cloud)
    first = scan(cloud)
    cloud.remote.files['11']['name'] = '改名 (2024) [tmdb-42]'
    cloud.remote.files['12'].update(parent_id='14',size=2000)
    second = scan(cloud)
    assert first != second
    result = inventory.listing(cloud.id)
    assert result['scan_job'] == second
    assert next(row for row in result['items'] if row['cid'] == '11')['video_count'] == 0
    tv = next(row for row in result['items'] if row['cid'] == '14')
    assert tv['video_count'] == 4 and tv['size'] == 5072


@pytest.mark.parametrize('failure',['list','stop','revision','profile'])
def test_incomplete_scan_keeps_previous_snapshot(cloud,monkeypatch,failure):
    tree(cloud)
    first = scan(cloud)
    jid = inventory.start(cloud.id,'0',enrich=False)
    if failure == 'stop':
        original = cloud.remote.list
        def stopped(cid):
            rows = original(cid)
            jobs.request_stop(jid)
            return rows
        monkeypatch.setattr(cloud.remote,'list',stopped)
    elif failure == 'list':
        monkeypatch.setattr(cloud.remote,'list',Mock(side_effect=CloudError('page failed',502)))
    elif failure == 'profile':
        monkeypatch.setattr(cloud.remote,'profile',Mock(side_effect=CloudError('profile failed',502)))
    else:
        accounts.save_verified('UID=123_A1_1; CID=changed; SEID=changed',cloud.remote)
    jobs.execute(jid)
    assert store.job(jid)['state'] in ('stopped','failed')
    result = inventory.listing(cloud.id)
    if failure == 'revision':
        assert not result['has_snapshot']
    else:
        assert result['scan_job'] == first
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_inventory_items WHERE job_id=?',(jid,)).fetchone()


def test_running_snapshot_is_not_published_before_job_success(cloud):
    tree(cloud)
    first = scan(cloud)
    jid = inventory.start(cloud.id,'0',enrich=False)
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='running' WHERE id=?",(jid,))
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(jid,)).fetchone()[0])
    result = inventory.run(jid, payload)
    assert result['inventory']['media_count'] == 2
    assert inventory.listing(cloud.id)['scan_job'] == first


def test_dedupe_and_account_isolation(cloud):
    tree(cloud)
    jid = inventory.start(cloud.id,'0',enrich=False)
    assert inventory.start(cloud.id,'0',enrich=False) == jid
    with pytest.raises(CloudError) as error:
        inventory.start(cloud.id,'0',kind_hint='movie',enrich=False)
    assert error.value.status == 409
    jobs.execute(jid)
    other = accounts.save_authorization('999',{}, {'cookie':'UID=999_H1_1; CID=x; SEID=y'})
    assert not inventory.listing(other)['has_snapshot']
    store.disconnect_account(cloud.id)
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_inventory_items WHERE account_id=?',(cloud.id,)).fetchone()


@pytest.mark.parametrize('name,season,expected',[
    ('X.S00E01.mkv',None,[(0,1)]), ('X.S01E01-E03.mkv',None,[(1,1),(1,2),(1,3)]),
    ('X.S01E01E03.mkv',None,[(1,1),(1,3)]), ('[02].mkv',0,[(0,2)]),
    ('E01-E03.mkv',0,[(0,1),(0,2),(0,3)]), ('Show.S01E1000.mkv',None,[(1,1000)]),
    ('[02].mkv',None,[]), ('第2季第03集.mkv',None,[(2,3)]),
    ('Show.S01E001-E999.mkv',None,[]), ('Movie.2024.1080p.mkv',None,[]),
])
def test_episode_evidence(name,season,expected):
    assert episodes(name,season) == expected


def test_unknown_and_loose_files_are_not_guessed():
    root={'id':'0','parent_id':'0','name':'全部文件'}
    rows=[{'id':'1','parent_id':'0','name':'Unknown'}]
    files=[{'id':'2','parent_id':'1','name':'movie.mkv'}, {'id':'3','parent_id':'0','name':'loose.mp4'}]
    result=collect(root,rows,files,{'.mkv','.mp4'})
    assert result['unassigned_videos']==1
    assert result['items'][0]['kind']=='' and result['items'][0]['tmdb_id']==''


def test_inventory_api_validates_and_dispatches(cloud,client):
    endpoint=f'/api/cloud/accounts/{cloud.id}/inventory'
    for payload in ({'kind_hint':'guess'}, {'enrich':'true'}, {'root_cid':'../../escape'}, {'extra':'x'}):
        assert client.post(endpoint+'/scan',json=payload).status_code == 400
    for qs in ('?limit=101','?offset=-1','?kind=bad'):
        assert client.get(endpoint+qs).status_code==422
    response=client.post(endpoint+'/scan',json={'root_cid':'0','enrich':False})
    assert response.status_code==202
    assert client.get(endpoint).json()['active_job']==response.json()['job_id']
    client.headers['Origin']='https://evil.example'
    assert client.post(endpoint+'/scan',json={}).status_code==403
    client.cookies.clear()
    assert client.get(endpoint).status_code==401


def test_metadata_requests_are_bounded_and_duplicate_ids_share_cache(cloud,monkeypatch):
    tree(cloud)
    for index in range(201):
        fid=str(1000+index)
        cloud.remote.files[fid]={'id':fid,'parent_id':'10','name':f'电影{index} [tmdb-{1000+index}]','is_dir':True,'size':0}
    cloud.remote.files['2000']={'id':'2000','parent_id':'10','name':'同编号 [tmdb-42]','is_dir':True,'size':0}
    tmdb=Mock()
    tmdb.get.side_effect=lambda path:{'id':int(path.rsplit('/',1)[1]),'title':path}
    monkeypatch.setattr(recognition,'tmdb',lambda:tmdb)
    jid=inventory.start(cloud.id,'0',enrich=True)
    jobs.execute(jid)
    assert store.job(jid)['state']=='success'
    assert tmdb.get.call_count==200
    result=inventory.listing(cloud.id)
    assert result['summary']['metadata_pending'] > 0
    assert result['counts']['all']==204


@pytest.mark.parametrize('interrupt',['stop','revision'])
def test_publish_transaction_rechecks_account_and_stop(cloud,monkeypatch,interrupt):
    tree(cloud)
    first=scan(cloud)
    original=jobs.step
    def changed(job_id,key,payload,fn):
        if interrupt=='stop':
            jobs.request_stop(job_id)
        else:
            accounts.save_verified('UID=123_A1_1; CID=changed; SEID=changed',cloud.remote)
        return original(job_id,key,payload,fn)
    monkeypatch.setattr(jobs,'step',changed)
    jid=scan(cloud)
    assert store.job(jid)['state'] in ('failed','stopped')
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_inventory_items WHERE job_id=?',(jid,)).fetchone()
    if interrupt=='stop':
        assert inventory.listing(cloud.id)['scan_job']==first


def test_empty_success_is_an_empty_inventory_not_a_cleanup_request(cloud):
    tree(cloud)
    scan(cloud)
    cloud.remote.files={'0':cloud.remote.files['0']}
    jid=scan(cloud)
    result=inventory.listing(cloud.id)
    assert result['scan_job']==jid and result['has_snapshot'] and result['total']==0
    assert cloud.remote.writes==[]
