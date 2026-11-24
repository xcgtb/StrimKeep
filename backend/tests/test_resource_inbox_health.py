from types import SimpleNamespace

from strimkeep.resource_inbox import ResourceInbox
from strimkeep.store import Store


def test_feed_backoff_and_inbox_prune(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    inbox = ResourceInbox(SimpleNamespace(store=store, http=None))
    try:
        now = 1_000_000.0
        with store.db:
            feed_id = store.db.execute(
                'INSERT INTO resource_feeds(name,url,enabled,interval,next_run,created,updated)'
                " VALUES('test','https://example.com/rss',1,300,?,?,?)",
                (now, now, now)).lastrowid
            for index in range(3):
                store.db.execute(
                    'INSERT INTO resource_inbox(source,dedupe_key,title,text,status,created,updated)'
                    " VALUES('rss',?,'old','x','skipped',?,?)",
                    ('old-%d' % index, now - 10 * 86400, now - 10 * 86400))
        feed = store.db.execute('SELECT * FROM resource_feeds WHERE id=?', (feed_id,)).fetchone()
        inbox._feed_done(feed, now, state='error', error='boom')
        failed = store.db.execute('SELECT * FROM resource_feeds WHERE id=?', (feed_id,)).fetchone()
        assert failed['failures'] == 1
        assert failed['next_run'] > now + 300
        inbox._feed_done(failed, now, state='ok', accepted=2)
        ok = store.db.execute('SELECT * FROM resource_feeds WHERE id=?', (feed_id,)).fetchone()
        assert ok['failures'] == 0 and ok['last_count'] == 2
        pruned = inbox.prune(keep_days=1, keep_rows=1)
        assert pruned['by_age'] == 3
        assert store.db.execute("SELECT count(*) FROM resource_inbox WHERE status='skipped'").fetchone()[0] == 0
    finally:
        store.close()
