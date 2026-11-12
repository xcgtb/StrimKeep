import asyncio,json
import pytest
from strimkeep.cards import media_card
from test_organize import setup,new_job


def variety_card():
    return [({'name':f'现在就出发.2023.S00E{n}.2160p.WEB-DL.H.265-WF.mkv','size':1024**3},
             {'hint':{'media_type':'tv','season':0,'episode':n},'details':{'id':231620,'name':'现在就出发','first_air_date':'2023-07-30','backdrop_path':'/backdrop.jpg','seasons':[{'season_number':0,'episode_count':150}]},'parts':['综艺','国产综艺'],'relative_path':'影视媒体库/secret/path.strm','media':{'width':3840,'height':2160,'dynamic_range':'SDR','codec':'hevc'}}) for n in range(147,151)]


def test_variety_card_matches_reference_layout_and_excludes_debug_fields():
    title,body,photo=media_card(variety_card(),shared=True)
    assert title=='🎬 整理完成：现在就出发 (2023)'
    assert body=='''📺 本批：S00 E147-E150
🎞️ 画质：WEB-DL.SDR 2160p
🗂️ 分类：综艺 / 国产综艺
🔗 来源：115网盘分享
📁 文件：4 个
🗓️ 本季：S00 共 150 集
💾 体积：4.00G
🆔 编号：TMDB 231620
👥 组别：WF'''
    assert photo=='https://image.tmdb.org/t/p/w780/backdrop.jpg'
    assert all(x not in title+body for x in ['Emby','secret','未知','hevc','任务 #'])


def test_movie_card_uses_known_spec_and_does_not_invent_season_or_group():
    title,body,photo=media_card([({'name':'Finding.Nemo.2003.2160p.BluRay.mkv','size':44.38*1024**3},{'hint':{'media_type':'movie'},'details':{'id':12,'title':'海底总动员','release_date':'2003-05-30','poster_path':'/poster.jpg'},'parts':['电影','欧美电影'],'media':{'width':3840,'height':1600,'dynamic_range':'HDR PQ','codec':'hevc'}})])
    assert title=='🎬 整理完成：海底总动员 (2003)'
    assert '🎞️ 画质：BLURAY.HDR 2160p' in body and '💾 体积：44.38G' in body
    assert '组别：未知' in body and '本季' not in body and 'HDR10' not in body
    assert photo=='https://image.tmdb.org/t/p/w500/poster.jpg'


def test_combined_and_noncontiguous_episodes_count_actual_files():
    items=variety_card();items[0][1]['hint'].update(episode=1,end_episode=2);items=items[:1]+items[3:]
    _,body,_=media_card(items)
    assert body.startswith('📺 本批：S00 E01-E02、E150\n') and '📁 文件：2 个' in body


def test_missing_size_is_not_displayed_as_zero(tmp_path):
    items=variety_card()[:2]
    items[0][0]['size']=0
    _,body,_=media_card(items)
    assert '体积：1.00G（部分未知）' in body
    items[1][0]['size']=0
    _,body,_=media_card(items)
    assert '体积：未知' in body


def test_long_metadata_card_fits_telegram_caption_without_cutting_footer():
    items=variety_card();items[0][1]['details']['name']='🦊'*500;items[0][1]['parts']=['非常长的分类'*100,'地区'*100]
    title,body,_=media_card(items)
    assert len((title+'\n'+body).encode('utf-16-le'))//2<=1000
    assert '组别：WF' in body


def test_actual_notification_payload_uses_card_and_respects_disabled_details(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='测试电影 (2024).mkv')
        jid=new_job(store,{'source_cids':['100'],'archive_cid':'200','pending_cid':'300','directory_labels':{},'retries':1,'strm_delay':0,'notify_emby':False});await engine.run_job(jid)
        plan=json.loads(store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]);plan['details']['backdrop_path']='/backdrop.jpg'
        store.db.execute('UPDATE organize_items SET plan=? WHERE job_id=?',(json.dumps(plan),jid));store.db.commit()
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'strm_summary':False,'strm_files':False,'task':False,'emby':False}}})
        sent=[]
        async def send(token,method,payload):
            import httpx
            sent.append((method,payload));return httpx.Response(200,json={'ok':True},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        store.db.execute("DELETE FROM notifications WHERE event_key LIKE 'show_done:%'")
        await engine.notifications.job(engine.job(jid));await engine.notifications.drain_media();await engine.notifications.job(engine.job(jid));await engine.notifications.drain_media()
        assert len(sent)==1 and sent[0][0]=='sendPhoto'
        assert sent[0][1]['caption'].startswith('🎬 整理完成：') and '/t/p/w780/' in sent[0][1]['photo']
        assert '新片入库' not in sent[0][1]['caption'] and '尚未确认入库' not in sent[0][1]['caption']
        await http.aclose();store.close()
    asyncio.run(run())


def test_static_bundle_is_compressed_without_wrapping_api_responses(tmp_path,monkeypatch):
    import re
    from pathlib import Path as _P
    if not (_P(__file__).resolve().parents[2] / 'frontend' / 'dist').is_dir():
        pytest.skip('需要先构建前端产物（frontend/dist），仓库直跑时跳过')
    from fastapi.testclient import TestClient
    from test_console060 import console_app,login
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client)
        page=client.get('/')
        bundle=re.search(r'src="(/assets/[^\"]+\.js)"',page.text)[1]
        response=client.get(bundle,headers={'Accept-Encoding':'gzip'})
        assert response.status_code==200 and response.headers['Content-Encoding']=='gzip'
        assert len(response.content)>1024
        assert 'Content-Encoding' not in client.get('/api/v1/console/config').headers


def test_card_marks_share_strm_apart_from_cloud_files():
    """免转存的分享 STRM 标「115网盘分享」；转存后与网盘原有文件统一标「115网盘」。"""
    _, shared, _ = media_card(variety_card(), shared=True)
    _, transferred, _ = media_card(variety_card(), transferred=True)
    _, cloud, _ = media_card(variety_card())
    assert '来源：115网盘分享' in shared
    assert '来源：115网盘' in transferred and '分享' not in transferred
    assert '来源：115网盘' in cloud and '分享' not in cloud


def test_season_card_explains_skipped_and_replaced_episodes_without_claiming_gaps():
    items=variety_card()[:2]
    items[0][1]['hint']['episode']=1;items[1][1]['hint']['episode']=3
    items[1][1]['share_wash_replaced']=True
    other=[({'name':'现在就出发.2023.S00E02.mkv'}, {}, 'skipped', '已有同集或同名条目，保留已有')]
    title,body,_=media_card(items,shared=True,emby_status='pending',other_items=other)
    assert '本批：S00 E01、E03' in body and '保留已有：E02' in body
    assert '洗版：E03（替换旧版）' in body and '缺集' not in body
    assert '状态：Emby 入库待核实' in body

def test_long_card_keeps_emby_status_within_caption_limit():
    items=variety_card();items[0][1]['details']['name']='🦊'*500;items[0][1]['parts']=['🐱'*200,'🐱'*200]
    title,body,_=media_card(items,emby_status='pending',unassigned=20)
    assert len((title+'\n'+body).encode('utf-16-le'))//2<=1000
    assert '状态：Emby 入库待核实' in body


def test_remux_takes_precedence_only_for_actual_remux():
    from strimkeep.cards import version
    media = {'width': 3840, 'height': 2160, 'dynamic_range': 'HDR10'}
    assert version({'name': 'Movie.2160p.BluRay.REMUX.HDR.mkv'}, media) == 'REMUX.HDR10 2160p'
    assert version({'name': 'Movie.2160p.BDREMUX.mkv'}, media) == 'REMUX.HDR10 2160p'
    assert version({'name': 'Movie.2160p.BluRay.mkv'}, media) == 'BLURAY.HDR10 2160p'
    assert version({'name': 'Movie.2160p.WEB-DL.mkv'}, media) == 'WEB-DL.HDR10 2160p'
    assert version({'name': 'Movie.2160p.BluRay.Remuxed.mkv'}, media) == 'BLURAY.HDR10 2160p'


def test_outcome_lines_compress_kept_episodes_into_one_range():
    """保留已有按区间显示：逐集列出会被 100 字截断，后面的集数根本看不到。"""
    from strimkeep.cards import outcome_lines
    other=[({'name':f'尚公主.2026.S01E{n:02d}.2160p.WEB-DL.HDR.H.265.10-bit.60fps.DDP 5.1.mkv'}, {},
            'skipped', '已有同集或同名条目，保留已有') for n in range(1, 27)]
    assert outcome_lines(other, 1) == ['📌 保留已有：E01-E26']


def test_outcome_lines_keep_gaps_apart_without_listing_every_episode():
    from strimkeep.cards import outcome_lines
    other=[({'name':f'尚公主.2026.S01E{n:02d}.mkv'}, {}, 'skipped', '已有同集或同名条目，保留已有')
           for n in list(range(1, 26)) + [27]]
    assert outcome_lines(other, 1) == ['📌 保留已有：E01-E25、E27']


def test_outcome_lines_count_skips_without_episode_numbers_instead_of_calling_them_missing():
    """S01E16-1080P.mkv 读不出作品名和集号：只报数量，不能说成「未入库」。"""
    from strimkeep.cards import outcome_lines
    other=[({'name':f'S01E{n:02d}-1080P.mkv'}, {}, 'skipped', '已有同集或同名条目，保留已有')
           for n in range(1, 19)]
    lines=outcome_lines(other, 1)
    assert lines == ['📌 保留已有：18 项（无集号）'] and '未入库' not in lines[0]
