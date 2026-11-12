"""用户会话读取：机器人按钮里的链接、会话列表里的群组和机器人。

背景（0.9.45）：资源机器人把分享链接放在内联按钮里，正文是空的。只遍历
`message.entities` 会一条都抓不到 —— 用户加了机器人，结果什么都不来。
"""
import asyncio

from telethon.tl.types import MessageEntityTextUrl

from strimkeep.telegram_session import TelegramSession


class FakeEntity:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeButton:
    def __init__(self, url):
        self.url = url


class FakeRow:
    def __init__(self, *buttons):
        self.buttons = list(buttons)


class FakeMarkup:
    def __init__(self, *rows):
        self.rows = list(rows)


class FakeMessage:
    def __init__(self, mid, text='', entities=None, reply_markup=None):
        self.id, self.message = mid, text
        self.entities = entities or []
        self.reply_markup = reply_markup


class FakeDialog:
    def __init__(self, id, title, entity, is_channel=False, is_group=False):
        self.id, self.title, self.entity = id, title, entity
        self.is_channel, self.is_group = is_channel, is_group


class FakeClient:
    def __init__(self, messages=None, dialogs=None, entity=None):
        self.messages, self.dialogs, self.entity = messages or [], dialogs or [], entity

    async def get_entity(self, target):
        return self.entity

    async def get_messages(self, entity, limit=20, max_id=0):
        return list(self.messages)

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog


def session_with(client):
    engine = TelegramSession.__new__(TelegramSession)
    engine.store = None
    engine._client = client
    return engine


def test_button_links_are_read_when_the_text_is_empty():
    async def run():
        message = FakeMessage(7, text='', reply_markup=FakeMarkup(
            FakeRow(FakeButton('https://115.com/s/swabc123?password=abcd'),
                    FakeButton('https://t.me/other'))))
        engine = session_with(FakeClient([message], entity=FakeEntity(title='资源机器人')))
        parsed = await engine.fetch('123456789')
        assert parsed['title'] == '资源机器人'
        assert parsed['messages'][0]['text'] == ''
        assert parsed['messages'][0]['links'] == ['https://115.com/s/swabc123?password=abcd',
                                                  'https://t.me/other']
    asyncio.run(run())


def test_button_link_is_not_repeated_when_the_text_already_has_it():
    async def run():
        text = '今天更新 https://115.com/s/swabc123?password=abcd'
        entity = MessageEntityTextUrl(offset=5, length=42, url='https://115.com/s/swabc123?password=abcd')
        message = FakeMessage(8, text=text, entities=[entity], reply_markup=FakeMarkup(
            FakeRow(FakeButton('https://115.com/s/swabc123?password=abcd'))))
        engine = session_with(FakeClient([message], entity=FakeEntity(title='频道')))
        parsed = await engine.fetch('123456789')
        assert parsed['messages'][0]['links'] == ['https://115.com/s/swabc123?password=abcd']
    asyncio.run(run())


def test_message_without_text_or_links_is_still_skipped():
    async def run():
        message = FakeMessage(9, text='', reply_markup=FakeMarkup(FakeRow(FakeButton(''))))
        engine = session_with(FakeClient([message], entity=FakeEntity(title='频道')))
        parsed = await engine.fetch('123456789')
        assert parsed['messages'] == []
    asyncio.run(run())


def test_dialogs_list_channels_groups_and_bots_but_not_contacts():
    async def run():
        dialogs = [
            FakeDialog(-1001234567890, '私密群组', FakeEntity(username=''), is_group=True),
            FakeDialog(555, '资源机器人', FakeEntity(username='res_bot', bot=True)),
            FakeDialog(100, '公开频道', FakeEntity(username='pubchan', bot=False), is_channel=True),
            FakeDialog(666, '普通联系人', FakeEntity(username='someone', bot=False)),
        ]
        engine = session_with(FakeClient(dialogs=dialogs))
        items = await engine.dialogs()
        assert {item['title']: item['kind'] for item in items} == {
            '公开频道': 'channel', '私密群组': 'group', '资源机器人': 'bot'}
        assert [item['kind'] for item in items] == ['channel', 'group', 'bot']
    asyncio.run(run())
