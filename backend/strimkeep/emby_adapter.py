"""Emby 的 MediaServer 端口实现。

只做只读/刷新能力；业务层仍然通过 EmbyDelivery 处理带身份的交付确认，
这个 adapter 服务于统一入口与未来的 Jellyfin 替换，不改变现有确认规则。
"""
from .emby_connection import saved_base
from .network import emby_request
from .ports import MediaServer


class EmbyMediaServer(MediaServer):
    name = 'emby'

    def __init__(self, store, http):
        self.store = store
        self.http = http

    def connection(self):
        return saved_base(self.store)

    async def test(self):
        connection = self.connection()
        if not connection:
            return False, '尚未配置 Emby 地址或 API Key'
        url, key = connection
        try:
            response = await emby_request(self.http, 'GET', url + '/System/Info',
                                          headers={'X-Emby-Token': key}, timeout=10)
            response.raise_for_status()
            return True, ''
        except Exception as exc:
            return False, type(exc).__name__

    async def scan(self, paths):
        connection = self.connection()
        if not connection:
            return
        url, key = connection
        updates = [{'Path': p, 'UpdateType': 'Modified'} for p in paths if p]
        for start in range(0, len(updates), 200):
            response = await emby_request(
                self.http, 'POST', url + '/Library/Media/Updated',
                headers={'X-Emby-Token': key}, json={'Updates': updates[start:start + 200]}, timeout=15)
            response.raise_for_status()

    async def items(self, query):
        connection = self.connection()
        if not connection:
            return []
        url, key = connection
        params = {'Recursive': 'true', 'Fields': 'Path,ProviderIds,MediaSources', **query}
        response = await emby_request(self.http, 'GET', url + '/Items',
                                      headers={'X-Emby-Token': key}, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data.get('Items') if isinstance(data, dict) and isinstance(data.get('Items'), list) else []

    async def delete(self, item_id):
        connection = self.connection()
        if not connection:
            return False
        url, key = connection
        response = await emby_request(self.http, 'DELETE', url + '/Items/' + str(item_id),
                                      headers={'X-Emby-Token': key}, timeout=15)
        return 200 <= response.status_code < 300
