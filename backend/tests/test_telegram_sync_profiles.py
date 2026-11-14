import asyncio
import json
import hashlib
from types import SimpleNamespace

from strimkeep.store import Store
from strimkeep.tasks import Tasks
from strimkeep.telegram_bot import TelegramBot, sync_profile_revision


class Response:
    status_code=200
    def raise_for_status(self): pass
    def json(self): return {'ok':True}


class Notifications:
    def __init__(self): self.sent=[]; self.notices=[]
    async def send(self, token, method, payload):
        self.sent.append((method,payload)); return Response()
    async def publish(self,*args,**kwargs): self.notices.append((args,kwargs))


class Http:
    def __init__(self): self.calls=[]
    async def post(self,url,**kwargs): self.calls.append((url,kwargs)); return Response()


def message(uid=1, text='/sync', sender=42):
    return {'update_id':uid,'message':{'text':text,'from':{'id':sender},'chat':{'id':sender,'type':'private'}}}


def callback(uid, p, callback_id='click1', sender=42, revision=None):
    return {'update_id':uid,'callback_query':{'id':callback_id,'from':{'id':sender},
        'message':{'chat':{'id':sender,'type':'private'}},
        'data':f"sync:{p['id']}:{revision or sync_profile_revision(p)}"}}


def setup(tmp_path):
    store=Store(tmp_path/'data','admin','test-password-1234')
    profiles=[{'id':'a1b2c3d4','name':'影视增量','source_cid':'100','source_label':'电影',
               'source_cids':['100','200'],'source_labels':{'100':'电影','200':'剧集'},'mode':'incremental'},
              {'id':'11223344','name':'影视全量','source_cid':'100','source_label':'电影','mode':'full'}]
    store.set_many({'sync_profiles':profiles,'cookie':'UID=1; CID=t; SEID=t',
                    'playback_base':'http://nas:9000','notification_config':{'bot_token':'test'}})
    notes=Notifications(); http=Http()
    engine=SimpleNamespace(store=store,http=http,notifications=notes)
    engine.tasks=Tasks(engine)
    return store,profiles,notes,http,TelegramBot(engine)


def test_menu_and_callback_use_the_same_profiles_as_web(tmp_path):
    async def run():
        store,profiles,notes,http,bot=setup(tmp_path); config={'allowed_user_id':'42'}
        await bot.handle(message(),config,'bot1')
        assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()
        method,payload=notes.sent[0]
        assert method=='sendMessage' and len(payload['reply_markup']['inline_keyboard'])==2
        assert '增量' in payload['reply_markup']['inline_keyboard'][0][0]['text']
        assert '全量' in payload['reply_markup']['inline_keyboard'][1][0]['text']
        await bot.handle(callback(2,profiles[0]),config,'bot1')
        row=store.db.execute('SELECT id,config FROM organize_jobs').fetchone(); details=json.loads(row['config'])
        assert details['source_cids']==['100','200'] and details['mode']=='incremental'
        assert details['sync_profile_id']==profiles[0]['id'] and not details['scheduled']
        assert '增量同步' in notes.notices[-1][0][2] and '#'+str(row['id']) in notes.notices[-1][0][2]
        await bot.handle(callback(2,profiles[0]),config,'bot1')
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==1
        await bot.handle(callback(3,profiles[1],callback_id='click2'),config,'bot1')
        full=json.loads(store.db.execute('SELECT config FROM organize_jobs ORDER BY id DESC').fetchone()[0])
        assert full['mode']=='full' and full['_force_full'] is True and full['source_cids']==['100']
        assert any('answerCallbackQuery' in url for url,_ in http.calls)
        store.close()
    asyncio.run(run())


def test_callback_checks_authorization_stale_tasks_and_command_setting(tmp_path):
    async def run():
        store,profiles,notes,http,bot=setup(tmp_path); config={'allowed_user_id':'42'}
        await bot.handle(callback(1,profiles[0],sender=99),config,'bot1')
        await bot.handle(callback(2,profiles[0],callback_id='stale',revision='0000000000'),config,'bot1')
        store.set_many({'bot_commands_config':{'sync':False}})
        await bot.handle(callback(3,profiles[0],callback_id='disabled'),config,'bot1')
        assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()
        assert not notes.notices and len(http.calls)==2
        store.close()
    asyncio.run(run())


def test_sync_menu_empty_does_not_fall_back_to_old_settings(tmp_path):
    async def run():
        store,profiles,notes,http,bot=setup(tmp_path)
        store.set_many({'sync_profiles':[],'sync_config':{'source_cids':['100']}})
        await bot.handle(message(),{'allowed_user_id':'42'},'bot1')
        assert '还没有同步任务' in notes.notices[0][0][2]
        assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()
        store.close()
    asyncio.run(run())


def test_menu_and_receipt_follow_saved_names_and_directories(tmp_path):
    async def run():
        store,profiles,notes,http,bot=setup(tmp_path)
        name='自定义电影资料库同步任务' * 5
        profiles[0].update(name=name, source_cids=['200'], source_labels={'200':'115 网盘 / 自选目录'})
        store.set_many({'sync_profiles':profiles})
        await bot.handle(message(),{'allowed_user_id':'42'},'bot1')
        assert notes.sent[0][1]['reply_markup']['inline_keyboard'][0][0]['text']==name+' · 增量'
        await bot.handle(callback(2,profiles[0]),{'allowed_user_id':'42'},'bot1')
        assert name in notes.notices[-1][0][2] and '115 网盘 / 自选目录' in notes.notices[-1][0][2]
        details=json.loads(store.db.execute('SELECT config FROM organize_jobs').fetchone()[0])
        assert details['source_cids']==['200']
        store.close()
    asyncio.run(run())


def test_poll_requests_and_processes_callback_updates(tmp_path):
    async def run():
        store,profiles,notes,http,bot=setup(tmp_path)
        config={'allowed_user_id':'42','bot_enabled':True}
        identity=hashlib.sha256(b'test').hexdigest()[:24]
        store.set_many({'transfer_config':config,'bot-menu:'+identity+':v4':'ready','bot-offset:'+identity:1})
        class PollResponse(Response):
            def json(self): return {'ok':True,'result':[callback(10,profiles[1],callback_id='poll-click')]}
        class PollHttp(Http):
            async def post(self,url,**kwargs):
                self.calls.append((url,kwargs))
                return PollResponse() if url.endswith('/getUpdates') else Response()
        http=PollHttp(); bot.http=http
        assert await bot.poll()
        request=next(args for url,args in http.calls if url.endswith('/getUpdates'))
        assert 'callback_query' in request['json']['allowed_updates']
        details=json.loads(store.db.execute('SELECT config FROM organize_jobs').fetchone()[0])
        assert details['mode']=='full' and details['sync_profile_id']==profiles[1]['id']
        assert store.get('bot-offset:'+identity)==11
        store.close()
    asyncio.run(run())
