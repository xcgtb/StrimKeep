import time
from types import SimpleNamespace

import pytest

from strimkeep.resource_inbox import ResourceInbox, _validate_url, candidates, feed_items
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


def test_rss_url_validation_blocks_local_and_metadata_targets():
    for value in ('http://localhost/rss', 'http://127.0.0.1/rss', 'http://[::1]/rss',
                  'http://169.254.169.254/latest/meta-data', 'http://metadata.google.internal/'):
        with pytest.raises(ValueError):
            _validate_url(value)
    assert _validate_url('https://example.com/feed.xml') == 'https://example.com/feed.xml'


def test_expired_processing_lease_is_reclaimed(tmp_path):
    import asyncio

    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')

    class Offline:
        async def submit(self, urls, target, label, organize, origin):
            return 9

    engine = SimpleNamespace(store=store, http=None, transfer=None, offline=Offline(), closing=False)
    inbox = ResourceInbox(engine)
    try:
        inbox.add_many('magnet:?xt=urn:btih:ABCDEF123456', 'manual', '', 'Movie', '123', False)
        with store.db:
            store.db.execute("UPDATE resource_inbox SET status='processing',lease_until=?",
                             (time.time() - 1,))
        asyncio.run(inbox.process_pending())
        row = store.db.execute('SELECT status FROM resource_inbox').fetchone()
        assert row['status'] == 'queued'
    finally:
        store.close()


def test_prune_keeps_rows_referenced_by_subscription_attempts(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    inbox = ResourceInbox(SimpleNamespace(store=store, http=None))
    try:
        result = inbox.add_many('magnet:?xt=urn:btih:ABCDEF123456', 'manual', '', 'Movie', '123', False)
        row_id = result['items'][0]
        with store.db:
            store.db.execute("UPDATE resource_inbox SET status='failed',updated=0 WHERE id=?", (row_id,))
            store.db.execute(
                """INSERT INTO media_subscription_attempts
                   (media_type,tmdb_id,season,episode,link_key,kind,inbox_id,created)
                   VALUES('movie',78,0,0,'magnet:abcdef123456','magnet',?,?)""",
                (row_id, time.time()))
        inbox.prune(keep_days=1, keep_rows=1)
        assert store.db.execute('SELECT 1 FROM resource_inbox WHERE id=?', (row_id,)).fetchone()
    finally:
        store.close()
