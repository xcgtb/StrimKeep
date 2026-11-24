from types import SimpleNamespace

from strimkeep.resource_inbox import ResourceInbox, candidates, feed_items
from strimkeep.store import Store


def test_candidates_normalize_share_and_offline_links():
    found = candidates(
        'https://115.com/s/abcd1234?password=efgh\n'
        'magnet:?xt=urn:btih:ABCDEF123456&dn=Movie\n'
        'ed2k://|file|Movie.mkv|123|0123456789abcdef0123456789abcdef|/')
    keys = [item[0] for item in found]
    assert keys == ['share:abcd1234', 'magnet:abcdef123456',
                    'ed2k:0123456789abcdef0123456789abcdef']
    assert found[0][1].endswith('?password=efgh')


def test_inbox_deduplicates_and_requeues_failed_rows(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    try:
        inbox = ResourceInbox(SimpleNamespace(store=store, http=None))
        first = inbox.add_many('https://115.com/s/abcd1234?password=efgh',
                               'manual', '', 'Movie', '123', False)
        assert first['accepted'] == 1 and first['duplicates'] == 0
        again = inbox.add_many('https://115.com/s/abcd1234?password=efgh',
                               'rss', 'guid-1', 'Movie', '123', False)
        assert again['accepted'] == 0 and again['duplicates'] == 1
        with store.db:
            store.db.execute("UPDATE resource_inbox SET status='failed'")
        retry = inbox.add_many('https://115.com/s/abcd1234?password=efgh',
                               'manual', '', 'Movie', '123', False)
        assert retry['accepted'] == 0 and retry['requeued'] == 1
        row = store.db.execute('SELECT * FROM resource_inbox').fetchone()
        assert row['status'] == 'pending' and row['attempts'] == 0
    finally:
        store.close()


def test_feed_items_ignores_article_link_but_keeps_magnet():
    xml = '''<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <title>Movie 2024</title>
      <link>https://example.com/article/123</link>
      <guid>post-123</guid>
      <description>1080p magnet:?xt=urn:btih:ABCDEF123456</description>
    </item></channel></rss>'''
    items = feed_items(xml)
    assert len(items) == 1
    assert items[0]['guid'] == 'post-123'
    assert 'magnet:?xt=urn:btih:ABCDEF123456' in items[0]['text']
    assert 'example.com/article' not in items[0]['text']


def test_worker_hands_links_to_existing_pipelines(tmp_path):
    import asyncio

    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    calls = []

    class Transfer:
        async def enqueue(self, text, target, auto, origin):
            calls.append(('transfer', text, target, auto, origin))
            return [7]

    class Offline:
        async def submit(self, urls, target, label, organize, origin):
            calls.append(('offline', urls, target, organize, origin))
            return 9

    engine = SimpleNamespace(store=store, http=None, transfer=Transfer(),
                             offline=Offline(), closing=False)
    inbox = ResourceInbox(engine)
    try:
        inbox.add_many('https://115.com/s/abcd1234?password=efgh',
                       'manual', '', 'Movie', '123', False)
        inbox.add_many('magnet:?xt=urn:btih:ABCDEF123456', 'manual', '', 'Movie', '123', False)
        rows = store.db.execute('SELECT * FROM resource_inbox ORDER BY id').fetchall()
        for row in rows:
            asyncio.run(inbox.process_row(row))
        assert [row['status'] for row in store.db.execute(
            'SELECT status FROM resource_inbox ORDER BY id')] == ['queued', 'queued']
        assert calls[0][0] == 'transfer' and calls[0][2] == '123'
        assert calls[1][0] == 'offline' and calls[1][2] == '123'
    finally:
        store.close()
