"""Isolated safety tests for batch deletion. No production files are touched."""
import importlib.util
import sys
import types
from pathlib import Path
import pytest

@pytest.fixture
def ctx(monkeypatch, tmp_path):
    # Load the new module against compatible minimal app stubs.
    app = types.ModuleType('app'); app.__path__=[str(Path(__file__).resolve().parents[2] / 'app')]
    engine = types.ModuleType('app.engine')
    pager = types.ModuleType('app.library_pagination')
    response = types.ModuleType('app.library_response')
    progress = types.ModuleType('app.deletion_progress')
    pager._lock = __import__('threading').RLock()
    pager._read = lambda: True
    revision = ['stable']
    response._key = lambda: revision[0]
    def data_row():
        return {'id':'s1','series_ids':['s1','s2'],'tmdb_id':'84842',
          'name':'国民老公','year':2018,'in_local':True,'in_share':True,
          'tmdb_info':{'match_status':'missing'}}
    pager._filtered = lambda media,status,scope,*args: [data_row()]
    engine.L_ROOT=tmp_path/'local'; engine.S_ROOT=tmp_path/'share'
    engine.L_ROOT.mkdir(); engine.S_ROOT.mkdir()
    class PM:
        def lib_of(self,path,fallback=False):
            return 'local' if path.startswith('/local/') else ('share' if path.startswith('/share/') else '')
    engine.EMBY_PATHS=PM()
    engine.emby_path_to_container=lambda p: (engine.L_ROOT/p.removeprefix('/local/')) if p.startswith('/local/') else ((engine.S_ROOT/p.removeprefix('/share/')) if p.startswith('/share/') else None)
    engine._inside=lambda path,root: path.is_relative_to(root)
    episodes=[]
    for sid, lib, numbers in [('s1','local',[1,2,3]),('s2','share',[1,2])]:
        for num in numbers:
            ep={'Id':f'{sid}-{num}', 'SeriesId':sid, 'Path':f'/{lib}/国民老公/S01E{num:02d}.strm'}
            episodes.append(ep)
            converted = engine.emby_path_to_container(ep['Path'])
            converted.parent.mkdir(parents=True, exist_ok=True)
            converted.write_text('fixture')
    series={sid:{'Id':sid,'Type':'Series','Name':'国民老公','ProductionYear':2018,
                  'ProviderIds':{'Tmdb':'84842'},'Path':f'/{lib}/国民老公'}
            for sid,lib in [('s1','local'),('s2','share')]}
    def request(route,params,**kwargs):
        assert route=='/Items'
        if 'Ids' in params:
            rows=[series[s] for s in params['Ids'].split(',') if s in series]
        else:
            # Simulates production Emby returning all of the same title regardless of ParentId.
            rows=list(episodes)
        return {'Items':rows,'TotalRecordCount':len(rows)}
    engine.emby_request=request
    accepted=[]
    def submit(op,identity,fn):
        accepted.append((identity,fn))
        return {'status':'accepted','operation_id':op}
    progress.submit=submit
    for name,obj in [('app',app),('app.engine',engine),('app.library_pagination',pager),
                     ('app.library_response',response),('app.deletion_progress',progress)]:
        monkeypatch.setitem(sys.modules,name,obj)
    for key,module in [('engine',engine),('library_pagination',pager),('library_response',response),('deletion_progress',progress)]:
        setattr(app,key,module)
    src=Path(__file__).with_name('batch_library_delete.py')
    if not src.exists(): src=Path(__file__).resolve().parents[2]/'app/batch_library_delete.py'
    spec=importlib.util.spec_from_file_location('app.batch_library_delete',src)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod,engine,series,episodes,revision,accepted,pager

def test_preview_local_and_share_exact_files(ctx):
    mod,*_=ctx
    a=mod.preview({'target':'local','scope':'local','filter':'missing','selection':'selected','series_ids':['s1']})
    b=mod.preview({'target':'share','scope':'share','filter':'all','selection':'all'})
    assert a['status']=='success' and a['strm_count']==3
    assert b['status']=='success' and b['strm_count']==2
    assert mod.preview({'target':'local','scope':'share','selection':'all'})['status']=='error'


def test_movie_batch_exact_target_identity_and_no_extra_files(ctx, monkeypatch):
    mod, engine, _, _, _, accepted, pager = ctx
    row = {'id': 'm1', 'ids': ['m1', 'm2'], 'tmdb_id': '7', 'name': '电影', 'year': 2024, 'in_local': True, 'in_share': True}
    pager._filtered = lambda *a: [row]
    items = [{'Id': ident, 'Type': 'Movie', 'Name': '电影', 'ProductionYear': 2024, 'ProviderIds': {'Tmdb': '7'}, 'Path': '/' + lib + '/movie.strm'} for ident, lib in [('m1', 'local'), ('m2', 'share')]]
    for item in items:
        engine.emby_path_to_container(item['Path']).write_text('fixture')
    engine.emby_request = lambda *a, **kw: {'Items': items, 'TotalRecordCount': 2}
    engine.find_movie_strms_by_tmdb = lambda root, tid: [root / 'movie.strm']
    body = {'media': 'movies', 'target': 'share', 'scope': 'share', 'selection': 'selected', 'movie_ids': ['m1']}
    pre = mod.preview(body)
    assert pre['status'] == 'success' and pre['strm_count'] == 1
    seen = []
    def delete(files, target, title, media_type, tmdb_id):
        seen.append((files, target, tmdb_id))
        return {'status': 'success', 'strm_removed': len(files)}
    reply = mod.execute({'target': 'share', 'confirm': True, 'confirmation': '删除分享库', 'preview_id': pre['preview_id'], 'operation_id': 'm' * 32}, delete)
    assert reply['status'] == 'accepted'
    accepted.pop()[1](lambda event: None)
    assert seen[0][1:] == ('share', '7') and all(p.is_relative_to(engine.S_ROOT) for p in seen[0][0])
    items[1]['ProviderIds']['Tmdb'] = '999'
    assert mod.preview(body)['status'] == 'error'
    items[1]['ProviderIds']['Tmdb'] = '7'
    engine.find_movie_strms_by_tmdb = lambda root, tid: [root / 'movie.strm', root / 'unverified.strm']
    assert mod.preview(body)['status'] == 'error'

def test_execute_requires_target_phrase_and_runs_only_target_paths(ctx):
    mod,engine,_,_,_,accepted,_=ctx
    pre=mod.preview({'target':'local','scope':'local','selection':'all'})
    assert pre['status']=='success'
    wrong=dict(target='local',preview_id=pre['preview_id'],confirm=True,confirmation='删除分享库',operation_id='a'*32)
    assert mod.execute(wrong, lambda *a,**k:None)['status']=='error'
    seen=[]
    def deleter(files,target,title,media_type,series_id):
        seen.append((target,list(files),series_id))
        return {'status':'success','strm_removed':len(files),'cloud_removed':len(files)}
    result=mod.execute(dict(wrong,confirmation='删除本地库及源文件'),deleter)
    assert result['status']=='accepted'
    info=accepted.pop()[1](lambda x: None)
    assert info['strm_removed']==3 and info['cloud_removed']==3
    assert len(seen)==1 and all(x.is_relative_to(engine.L_ROOT) for x in seen[0][1])
    assert mod.execute(dict(wrong,confirmation='删除本地库及源文件'),deleter)['status']=='error'

def test_conflicting_tmdb_rejected(ctx):
    mod,_,series,*_=ctx
    series['s2']['ProviderIds']['Tmdb']='99999'
    result=mod.preview({'target':'share','scope':'share','selection':'all'})
    assert result['status']=='error' and 'TMDB' in result['message']

def test_foreign_episodes_rejected(ctx):
    mod,_,_,episodes,*_=ctx
    episodes.append({'Id':'evil','SeriesId':'other','Path':'/share/foreign.strm'})
    result=mod.preview({'target':'share','scope':'share','selection':'all'})
    assert result['status']=='error' and '其他作品' in result['message']

def test_changed_files_skip_without_deletion(ctx):
    mod,engine,_,episodes,_,accepted,_=ctx
    pre=mod.preview({'target':'local','scope':'local','selection':'all'})
    assert pre['status']=='success'
    calls=[]
    body={'target':'local','preview_id':pre['preview_id'],'confirm':True,
          'confirmation':'删除本地库及源文件','operation_id':'b'*32}
    assert mod.execute(body,lambda *a,**k:calls.append(a))['status']=='accepted'
    engine.emby_path_to_container(episodes[0]['Path']).unlink()
    result=accepted[0][1](lambda _:None)
    assert result['failed']==1 and not calls

def test_stale_snapshot_rejected(ctx):
    mod,_,_,_,revision,_,_=ctx
    p=mod.preview({'target':'share','scope':'share','selection':'all'})
    revision[0]='new'
    result=mod.execute({'target':'share','preview_id':p['preview_id'],'confirm':True,
                        'confirmation':'删除分享库','operation_id':'c'*32},lambda *a,**k:None)
    assert result['status']=='error'

def test_status_scope_cannot_be_forged(ctx):
    mod,*rest=ctx
    assert mod.preview({'target':'local','scope':'all','selection':'all'})['status']=='error'
    assert mod.preview({'target':'local','scope':'local','selection':'all','filter':'not-real'})['status']=='error'
