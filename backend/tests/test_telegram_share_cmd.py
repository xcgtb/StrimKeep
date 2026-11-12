"""机器人 /shareorganize：手动触发待处理分享 STRM 的整理。"""
import asyncio
from types import SimpleNamespace

from strimkeep import share_service
from strimkeep.store import Store
from strimkeep.telegram_bot import TelegramBot


class Notices:
    def __init__(self):
        self.items = []

    async def publish(self, *args, **kwargs):
        self.items.append((args, kwargs))


def update(message_id=1, text='/shareorganize'):
    return {'update_id': message_id,
            'message': {'text': text, 'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}}}


def test_shareorganize_reports_empty_and_enqueues(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        share_service.install(store)
        notices = Notices()
        engine = SimpleNamespace(store=store, http=None, notifications=notices)
        bot = TelegramBot(engine)
        config = {'allowed_user_id': '42'}

        await bot.handle(update(1), config, 'bot1')
        assert '没有待整理的分享 STRM' in notices.items[0][0][2]

        with store.db:
            store.db.execute(
                "INSERT INTO share_strms(share_code,receive_code,file_id,pickcode,name,strm_path,"
                "token_hash,revoked,created,media,facts,origin,size,attachments) "
                "VALUES('s1','','f1','','n','%s/s1/n.strm','h',0,0,'{}','{}','test',0,'[]')"
                % share_service.PENDING_NAME)

        await bot.handle(update(2), config, 'bot1')
        assert '已入队' in notices.items[1][0][2]
        row = store.db.execute("SELECT config FROM organize_jobs ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None and 'share_organize' in row['config']
        store.close()

    asyncio.run(run())
