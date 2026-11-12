import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from strimkeep.store import Store
from strimkeep.telegram_bot import TelegramBot

class Notices:
    def __init__(self):self.items=[]
    async def publish(self,*args,**kwargs):self.items.append((args,kwargs))
class Offline:
    def __init__(self):self.calls=[]
    async def submit(self,urls,cid,label,origin='web',organize=True):
        # 0.8.40：submit 多带来源与「是否自动整理」，机器人必须传 origin='bot'。
        self.calls.append((urls,cid,label,origin,organize));return 42
class Transfer:
    def __init__(self):self.calls=[]
    async def enqueue(self,text,dest,auto,origin,identity):
        self.calls.append((text,dest,auto));return [43]

def message(text):
    return {'update_id':1,'message':{'text':text,'from':{'id':42},'chat':{'id':42,'type':'private'}}}
def setup_bot(tmp_path):
    store=Store(tmp_path/'data','admin','test-password-1234')
    store.set_many({'offline_config':{'bot_magnet_cid':'123','bot_magnet_label':'磁力目录','bot_ed2k_cid':'124','bot_ed2k_label':'电驴目录','directories_v2':True,'bot_auto_organize':True}})
    offline=Offline();transfer=Transfer();notices=Notices()
    engine=SimpleNamespace(store=store,http=None,offline=offline,transfer=transfer,notifications=notices,job=lambda _: {'config':'{"target_label":"分享目录"}'})
    return store,offline,transfer,notices,TelegramBot(engine)

def test_mixed_share_and_magnet_are_routed_independently(tmp_path):
    async def run():
        store,offline,transfer,notices,bot=setup_bot(tmp_path)
        await bot.handle(message('magnet:?xt=urn:btih:1234 ed2k://|file|a|1|hash|/ https://115.com/s/share123'),{'allowed_user_id':'42','bot_target_cid':'456'},'test')
        assert len(offline.calls)==2 and offline.calls[0][0]==['magnet:?xt=urn:btih:1234'] and offline.calls[0][1]=='123'
        assert offline.calls[1][0]==['ed2k://|file|a|1|hash|/'] and offline.calls[1][1]=='124'
        assert [c[3] for c in offline.calls]==['bot','bot'] and all(c[4] is True for c in offline.calls)
        assert len(transfer.calls)==1 and transfer.calls[0][1]=='456'
        assert len(notices.items)==2 and notices.items[0][0][0]!=notices.items[1][0][0]
        assert '批次 #42' in notices.items[0][0][2] and '#43' in notices.items[1][0][2]
        store.close()
    asyncio.run(run())

def test_bot_per_kind_organize_switches(tmp_path):
    # 0.8.58：磁力/ED2K 各自开关，显式设置优先于 bot_auto_organize。
    async def run():
        store,offline,transfer,notices,bot=setup_bot(tmp_path)
        store.set_many({'offline_config':{'bot_magnet_cid':'123','bot_magnet_label':'磁力目录','bot_ed2k_cid':'124','bot_ed2k_label':'电驴目录','directories_v2':True,'bot_auto_organize':True,'bot_magnet_auto_organize':False}})
        await bot.handle(message('magnet:?xt=urn:btih:1234 ed2k://|file|a|1|hash|/'),{'allowed_user_id':'42'},'test')
        assert [c[4] for c in offline.calls]==[False,True]
        store.close()
    asyncio.run(run())


def test_share_link_in_offline_command_routes_to_transfer(tmp_path):
    async def run():
        store,offline,transfer,notices,bot=setup_bot(tmp_path)
        await bot.handle(message('/offline https://115.com/s/share123'),{'allowed_user_id':'42','bot_target_cid':'456'},'test')
        assert not offline.calls and len(transfer.calls)==1 and '已接收' in notices.items[0][0][1]
        store.close()
    asyncio.run(run())

def test_share_strm_command_routes_multiple_shares_and_magnet(tmp_path):
    async def run():
        store,offline,transfer,notices,bot=setup_bot(tmp_path)
        with patch('strimkeep.share_service.enqueue_organize',side_effect=[71,72]) as enqueue:
            await bot.handle(message('/share https://115.com/s/one https://115.com/s/two magnet:?xt=urn:btih:1234'),{'allowed_user_id':'42'},'test')
        assert enqueue.call_count==2 and len(offline.calls)==1 and not transfer.calls
        assert len(notices.items)==2 and '#71、#72' in notices.items[0][0][2] and '批次 #42' in notices.items[1][0][2]
        store.close()
    asyncio.run(run())
