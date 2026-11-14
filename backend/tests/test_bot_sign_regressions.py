import asyncio
import unittest
from types import SimpleNamespace

from strimkeep.cloud115 import Cloud115, sign_details
from strimkeep.message_templates import cloud_sign_notice
from strimkeep.telegram_bot import TelegramBot, ordered_bot_commands


class Store:
    def __init__(self, values=None): self.values = values or {}
    def get(self, key, default=None): return self.values.get(key, default)
    def set_many(self, values): self.values.update(values)


class Response:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return {'ok': True, 'result': []}


class Http:
    def __init__(self): self.calls = []
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs['json']))
        return Response()


class Tests(unittest.TestCase):
    def test_sign_details_use_actual_today_and_continuous_day(self):
        data = {'continuous_day': 12, 'sign_list': [
            {'sign_day': '20261113', 'points_num': '11'},
            {'sign_day': '20261114', 'points_num': '12'}]}
        self.assertEqual(sign_details(data, '20261114'), (12, 12))
        self.assertEqual(sign_details(data, '20261115'), (12, None))
        self.assertIn('12 枫叶', cloud_sign_notice(False, 12, reward=12)[1])

    def test_successful_sign_survives_followup_read_failure(self):
        async def run():
            cloud = Cloud115.__new__(Cloud115)
            client = SimpleNamespace(user_points_sign='read', user_points_sign_post='post')
            cloud.client = lambda: client
            calls = []
            async def call(method, **kwargs):
                calls.append(method)
                if len(calls) == 1: return {'data': {'is_sign_today': '0'}}
                if method == 'post': return {'data': {}}
                raise RuntimeError('read failed')
            cloud.call = call
            result = await cloud.sign()
            self.assertTrue(result['signed'])
            self.assertIsNone(result['reward'])
            self.assertEqual(calls, ['read', 'post', 'read'])
        asyncio.run(run())

    def test_menu_order_and_disabled_commands_are_synced_and_updated(self):
        async def run():
            store = Store({'notification_config': {'bot_token': 'test'},
                           'transfer_config': {'bot_enabled': True},
                           'bot_commands_order': ['sign', 'sync'],
                           'bot_commands_config': {'sync': False}})
            http = Http()
            bot = TelegramBot(SimpleNamespace(store=store, http=http))
            self.assertEqual(ordered_bot_commands(store)[:2], ['sign', 'sync'])
            await bot.poll()
            menu = http.calls[0][1]['commands']
            self.assertEqual([x['command'] for x in menu[:2]], ['help', 'sign'])
            self.assertNotIn('sync', [x['command'] for x in menu])
            store.values['bot_commands_config'] = {}
            await bot.poll()
            menus = [body['commands'] for url, body in http.calls if url.endswith('setMyCommands')]
            self.assertEqual(len(menus), 2)
            self.assertEqual([x['command'] for x in menus[-1]][:3], ['help', 'sign', 'sync'])
        asyncio.run(run())


if __name__ == '__main__': unittest.main()
