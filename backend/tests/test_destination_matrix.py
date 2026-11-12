import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
from strimkeep.channel_monitor import normalize_rule
from strimkeep.offline_api import directory_config, route_target
from test_directory_readback import api_setup
from test_channel_monitor import make
from test_telegram_share_cmd import Notices, update
from strimkeep.telegram_bot import TelegramBot


def test_legacy_effective_values_are_copied_and_explicit_empty_stays_empty():
    old={'target_cid':'100','target_label':'直链','magnet_cid':'200','magnet_label':'磁力'}
    cfg=directory_config(old)
    assert (cfg['ed2k_cid'],cfg['bot_magnet_cid'],cfg['bot_ed2k_cid'])==('100','200','100')
    cfg.update(directories_v2=True,ed2k_cid='',bot_ed2k_cid='')
    assert directory_config(cfg)['ed2k_cid']==''
    assert directory_config(cfg)['bot_ed2k_cid']==''
    assert route_target([{'keywords':'4k','target_cid':'300','target_label':'电影'}],'4K film','100','')==('300','电影')


def test_legacy_channel_modes_do_not_enable_downloads():
    base={'name':'testchan','offline_cid':'100','offline_label':'目录'}
    skipped=normalize_rule({**base,'share_mode':'strm'})
    assert skipped['magnet_cid']==skipped['ed2k_cid']==''
    enabled=normalize_rule({**base,'share_mode':'transfer'})
    assert enabled['magnet_cid']==enabled['ed2k_cid']=='100'
    separate=normalize_rule({**base,'share_mode':'strm','magnet_cid':'200','ed2k_cid':''})
    assert (separate['magnet_cid'],separate['ed2k_cid'])==('200','')


def test_channel_strm_and_two_explicit_download_destinations(tmp_path):
    async def run():
        cfg={'enabled':True,'notify':False,'channels':[{'name':'testchan','share_mode':'strm',
             'magnet_cid':'100','magnet_label':'磁力','ed2k_cid':'200','ed2k_label':'电驴'}]}
        store,monitor,_,_=make(tmp_path,{},cfg)
        await monitor.consume('testchan','',{'id':1,'text':'magnet:?xt=urn:btih:abc ed2k://one','links':[]})
        calls=monitor.engine.offline.calls
        assert [(x['cid'],x['urls'],x['organize']) for x in calls]==[('100',['magnet:?xt=urn:btih:abc'],False),('200',['ed2k://one'],False)]
        store.close()
    asyncio.run(run())


def test_bot_mixed_links_use_independent_destinations(tmp_path):
    from strimkeep.store import Store
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234')
        store.set_many({'offline_config':{'directories_v2':True,'bot_magnet_cid':'100','bot_ed2k_cid':'200'}})
        calls=[]
        class Offline:
            async def submit(self,urls,cid,label,organize=False,origin='web'): calls.append((urls,cid))
        notices=Notices()
        bot=TelegramBot(SimpleNamespace(store=store,http=None,offline=Offline(),notifications=notices))
        await bot.handle(update(1,'magnet:?xt=urn:btih:abc ed2k://one'),{'allowed_user_id':'42'},'bot1')
        assert calls==[(['magnet:?xt=urn:btih:abc'],'100'),(['ed2k://one'],'200')]
        assert '已提交' in notices.items[0][0][2]
        store.close()
    asyncio.run(run())


def test_bot_share_uses_its_own_directory_and_command_overrides(tmp_path):
    from strimkeep.store import Store
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234')
        calls=[]
        class Transfer:
            async def enqueue(self,text,dest,*args):
                calls.append(dest)
                return []
        notices=Notices()
        bot=TelegramBot(SimpleNamespace(store=store,http=None,transfer=Transfer(),notifications=notices))
        cfg={'allowed_user_id':'42','target_cid':'100','bot_target_cid':'200','auto_organize':False}
        await bot.handle(update(1,'https://115.com/s/abc'),cfg,'bot1')
        await bot.handle(update(2,'/save 300 https://115.com/s/abc'),cfg,'bot1')
        assert calls==['200','300']
        store.close()
    asyncio.run(run())


def test_destination_api_preserves_other_rows_and_legacy_values(tmp_path,monkeypatch):
    app,_=api_setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        client.headers['X-CSRF-Token']=login['csrf']
        store=app.state.store
        store.set_many({'transfer_config':{'target_cid':'100','target_label':'Web','auto_organize':False},
                        'offline_config':{'target_cid':'100','magnet_cid':'200'},
                        'notification_config':{'chat_id':'42'},
                        'channel_monitor':{'enabled':False,'channels':[{'name':'testchan','share_mode':'strm','offline_cid':'300'}]}})
        data=client.get('/api/v1/transfer/destinations').json()['rows']
        web,bot=data[:2]
        assert (web['ed2k_cid'],bot['share_cid'],bot['magnet_cid'],bot['ed2k_cid'])==('100','100','200','100')
        bot.update(share_cid='200',share_label='机器人',magnet_cid='300',ed2k_cid='')
        response=client.put('/api/v1/transfer/destinations',json=bot)
        assert response.status_code==200,response.text
        web2,bot2,channel=response.json()['rows']
        assert web2==web
        assert (bot2['share_cid'],bot2['magnet_cid'],bot2['ed2k_cid'])==('200','300','')
        assert not store.get('transfer_config',{}).get('allowed_user_id')
        assert channel['share_mode']=='strm' and channel['magnet_cid']==channel['ed2k_cid']==''
        channel.update(magnet_cid='100',magnet_label='磁力',ed2k_cid='200',ed2k_label='电驴')
        response=client.put('/api/v1/transfer/destinations',json=channel)
        assert response.status_code==200,response.text
        web3,bot3,channel3=response.json()['rows']
        assert (web3,bot3)==(web2,bot2)
        assert (channel3['share_mode'],channel3['magnet_cid'],channel3['ed2k_cid'])==('strm','100','200')
        assert store.get('channel_monitor',{})['enabled'] is False
