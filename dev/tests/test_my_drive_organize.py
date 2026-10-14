"""Template/classification/unknown workflows using durable records and remote fixtures."""

from pathlib import Path
from types import SimpleNamespace
import pytest
from dev.tests.test_cloud_workbench import cloud, TMDB
from dev.tests.test_my_drive import client
from app.cloud import naming, categories, recognition, settings, workflow, jobs, directories, store, strm
from app.cloud.errors import CloudError


class Catalog(TMDB):
    def __init__(self, genre=0):
        self.genre=genre
    def get(self,path,**params):
        kind='tv' if '/tv' in path else 'movie'
        item={'id':44, 'name':'Series', 'title':'Movie', 'first_air_date':'2024-01-01', 'release_date':'2024-01-01', 'genres':[{'id':self.genre}], 'genre_ids':[self.genre], 'origin_country':['CN'], 'production_countries':[{'iso_3166_1':'CN'}]}
        item.pop('title' if kind=='tv' else 'name')
        if '/search/' in path:return {'results':[item]}
        if '/episode/' in path:
            return {'id':4401,'name':'Episode','season_number':int(path.split('/season/')[1].split('/')[0]),'episode_number':int(path.rsplit('/',1)[1])}
        return item


def test_template_movie_tv_and_literal_braces_use_real_season_fields(cloud,monkeypatch):
    monkeypatch.setattr(recognition,'tmdb',lambda:Catalog())
    settings.save({'naming_style':'template','movie_directory_template':'{title} ({year}) {{tmdb-{tmdb_id}}}', 'tv_directory_template':'{title} ({year}) {{tmdb-{tmdb_id}}}', 'season_directory_template':'Season{season:02}', 'movie_file_template':'{title}.{year}.{resolution}{ext}','tv_file_template':'{title}.{year}.S{season:02d}E{episode:02d}{ext}'})
    movie=recognition.recognize('Movie.2024.2160p.mkv')
    assert movie['directory']=='电影/Movie (2024) {tmdb-44}'
    assert movie['name']=='Movie.2024.2160p.mkv'
    tv=recognition.recognize('Series.2024.S01E02.mkv')
    assert tv['directory']=='电视剧/Series (2024) {tmdb-44}/Season01'
    assert tv['name']=='Series.2024.S01E02.mkv'
    settings.save({'rename_enabled':False})
    assert recognition.recognize('Series.2024.S1E2.1080p.mkv')['name']=='Series.2024.S1E2.1080p.mkv'


@pytest.mark.parametrize('template',['../{title}{ext}','{title.__class__}{ext}','{title!r}{ext}','{title:{year}}{ext}','{season:99999999d}{ext}','{title','{title}.mp4'])
def test_unsafe_or_extension_changing_templates_are_rejected(template,cloud):
    with pytest.raises(CloudError):settings.save({'movie_file_template':template})


def test_render_skips_empty_quality_fields_but_cannot_fake_container(cloud):
    config={**settings.get(),'naming_style':'template','movie_file_template':'{title}.{resolution}.{source}{ext}'}
    values={'kind':'movie','title':'Movie','resolution':'','source':'','ext':'.mkv','original_name':'Movie.mkv','year':'2024','tmdb_id':'44'}
    assert naming.paths(config,values)['name']=='Movie.mkv'
    config['movie_file_template']='{title}{ext}.mp4'
    with pytest.raises(CloudError):naming.paths(config,values)


@pytest.mark.parametrize('genre,kind,expected',[(16,'movie','动漫'),(99,'movie','纪录片'),(10764,'tv','综艺'),(10767,'tv','综艺'),(0,'movie','电影'),(0,'tv','剧集')])
def test_tmdb_classification_region_year_preserves_movie_tv(genre,kind,expected,cloud,monkeypatch):
    monkeypatch.setattr(recognition,'tmdb',lambda:Catalog(genre))
    settings.save({'classification_enabled':True,'region_classification':True,'year_classification':True})
    media=recognition.recognize('Series.2024.S01E01.mkv' if kind=='tv' else 'Movie.2024.mkv')
    assert media['kind']==kind and media['directory'].startswith(expected+'/中国大陆/2024/')


def test_custom_category_is_explicit_and_persists_in_asset_binding(cloud,monkeypatch):
    monkeypatch.setattr(recognition,'tmdb',lambda:Catalog())
    settings.save({'classification_enabled':True,'custom_categories':['短剧']})
    media=recognition.recognize('Movie.2024.mkv',{'kind':'movie','tmdb_id':'44','category':'短剧'})
    assert media['directory'].startswith('短剧/') and media['category_override']=='短剧'
    row = cloud.remote.info('4')
    strm.generate(cloud.id, row, media, provider=cloud.remote)
    assert workflow.media_choice(cloud.id, row, None)['category']=='短剧'
    with pytest.raises(CloudError):recognition.recognize('Movie.2024.mkv',{'kind':'movie','tmdb_id':'44','category':'未启用'})


def test_create_selected_categories_twice_reuses_directories(cloud,client):
    settings.save({'custom_categories':['短剧']})
    def run():
        response=client.post('/api/cloud/categories/create',json={'account_id':cloud.id,'target_cid':'2','confirmed':True})
        assert response.status_code==202
        job=response.json()['job_id'];jobs.execute(job)
        assert store.job(job)['state']=='success'
        return store.job(job)['result']['directories']
    first=run();second=run()
    assert first==second
    assert {i['name'] for i in first}==set(categories.DEFAULT+['短剧'])
    assert len([w for w in cloud.remote.writes if w[0]=='mkdir'])==6


def test_preview_is_read_only_and_does_not_save_unsaved_templates(cloud,client):
    before=settings.get()
    response=client.post('/api/cloud/naming/preview',json={'settings':{'movie_directory_template':'{title} ({year}) {{tmdb-{tmdb_id}}}'}})
    assert response.status_code==200
    assert '{tmdb-123456}' in response.json()['items'][0]['directory']
    assert settings.get()==before and not cloud.remote.writes


def test_settings_change_after_plan_prevents_write(cloud):
    planned=workflow.plan(cloud.id,'1','2')
    settings.save({'movie_folder':'另一个电影目录'})
    with pytest.raises(CloudError,match='重新预览'):workflow.apply(planned['id'],True)
    assert not cloud.remote.writes


def test_unknown_move_keeps_video_and_explicit_subtitles_names_and_no_strm(cloud,client):
    cloud.remote.files['4']['name']='Unclear.2024.mkv'
    cloud.remote.files['5']['name']='Unclear.2024.zh.srt'
    settings.save({'unknown_enabled':True})
    directories.save(cloud.id,{'unknown':'3'},0)
    planned=workflow.plan(cloud.id,'1','2')
    assert not planned['items'] and len(planned['unknown_items'])==1
    assert not cloud.remote.writes
    jid=workflow.apply(planned['id'],True);jobs.execute(jid)
    assert store.job(jid)['state']=='success'
    result=store.job(jid)['result']
    assert result['unknown'][0]['name']=='Unclear.2024.mkv' and not result['generated'] and not result['skipped']
    assert cloud.remote.info('4')['parent_id']==cloud.remote.info('5')['parent_id']
    assert cloud.remote.info(cloud.remote.info('4')['parent_id'])['parent_id']=='3'
    assert not list(cloud.root.rglob('*.strm'))


def test_unknown_off_keeps_original_location(cloud):
    cloud.remote.files['4']['name']='Unclear.2024.mkv'
    planned=workflow.plan(cloud.id,'1','2')
    assert planned['skipped'] and not planned['unknown_items']
    assert not cloud.remote.writes


def test_tmdb_transport_failure_never_moves_to_unknown(cloud,client,monkeypatch):
    settings.save({'unknown_enabled':True});directories.save(cloud.id,{'unknown':'3'},0)
    class Broken(TMDB):
        def get(self,*a,**kw):raise ValueError('bad JSON from upstream')
    monkeypatch.setattr(recognition,'tmdb',lambda:Broken())
    planned=workflow.plan(cloud.id,'1','2')
    assert planned['skipped'] and not planned['unknown_items'] and not cloud.remote.writes


def test_tv_transport_failure_is_not_a_semantic_unknown(cloud,client,monkeypatch):
    settings.save({'unknown_enabled':True});directories.save(cloud.id,{'unknown':'3'},0)
    cloud.remote.files['4']['name']='Series.2024.S01E01.mkv'
    class Broken(Catalog):
        def get(self,path,**params):
            if '/episode/' in path:raise ValueError('bad episode JSON')
            return super().get(path,**params)
    monkeypatch.setattr(recognition,'tmdb',lambda:Broken())
    planned=workflow.plan(cloud.id,'1','2')
    assert planned['skipped'] and not planned['unknown_items'] and not cloud.remote.writes


def test_template_organize_renames_companion_and_generates_matching_strm(cloud):
    settings.save({'naming_style':'template','movie_file_template':'{title}.{year}{ext}'})
    planned=workflow.plan(cloud.id,'1','2');jid=workflow.apply(planned['id'],True);jobs.execute(jid)
    assert store.job(jid)['state']=='success'
    assert cloud.remote.info('4')['name']=='Movie.2024.mkv'
    assert cloud.remote.info('5')['name']=='Movie.2024.zh.srt'
    assert list(cloud.root.rglob('Movie.2024.strm'))


def test_scan_excludes_nested_archive_directory(cloud):
    cloud.remote.files['2']['parent_id']='1'
    cloud.remote.files['6']={**cloud.remote.info('4'),'id':'6','parent_id':'2'}
    planned=workflow.plan(cloud.id,'1','2')
    assert {i['source']['id'] for i in planned['items']}=={'4'}
