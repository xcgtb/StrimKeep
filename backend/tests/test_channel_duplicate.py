"""频道判重：同一个频道的用户名 / 数字 ID 两种写法只抓一个。

背景：现场 `ltxjasna`（用户名）和 `-1004419698318`（数字 ID）是同一个频道 ——
频道原本公开，改成私密后用户名失效（`t.me/s/<用户名>` 变 302），于是又从用户
会话列表加了一条。按会话读取确认的 Telegram peer ID 判重。
"""
import sqlite3

from strimkeep.channel_monitor import ChannelMonitor


class Store:
    def __init__(self, conn, channels):
        self.db = conn
        self.events = []
        self.channels = list(channels)

    def get(self, key, default=None):
        if key == 'channel_monitor':
            return {'channels': [{'name': name} for name in self.channels]}
        return default

    def event(self, kind, message):
        self.events.append((kind, message))


def make_db(rows):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE channel_state(name TEXT PRIMARY KEY, title TEXT, last_message_id INTEGER,
                                   checked REAL, state TEXT, error TEXT, posts INTEGER, added REAL, peer_id TEXT);
    ''')
    for name, title, added, peer in rows:
        conn.execute("INSERT INTO channel_state VALUES (?,?,0,0,'ready','',0,?,?)", (name, title, added, peer))
    return conn


def engine_on(conn, channels):
    engine = ChannelMonitor.__new__(ChannelMonitor)
    engine.store = Store(conn, channels)
    return engine


def duplicates(conn):
    return dict(conn.execute("SELECT name,duplicate_of FROM channel_state").fetchall())


def test_same_peer_is_marked_duplicate():
    conn = make_db([('ltxjasna', 'Jasna LTX 频道', 1, '4419698318'), ('-1004419698318', 'Jasna LTX 新标题', 2, '4419698318')])
    engine = engine_on(conn, ['ltxjasna', '-1004419698318'])
    engine._mark_duplicates()
    assert duplicates(conn) == {'ltxjasna': None, '-1004419698318': 'ltxjasna'}
    assert any('同一个频道' in message for _, message in engine.store.events)


def test_matching_titles_on_different_peers_are_not_duplicates():
    conn = make_db([('chanA', '频道甲', 1, '1001'), ('chanB', '频道甲', 2, '2002')])
    engine = engine_on(conn, ['chanA', 'chanB'])
    engine._mark_duplicates()
    assert duplicates(conn) == {'chanA': None, 'chanB': None}
    assert engine.store.events == []


def test_untitled_channels_are_left_alone():
    conn = make_db([('chanA', '', 1, ''), ('chanB', '', 2, '')])
    engine = engine_on(conn, ['chanA', 'chanB'])
    engine._mark_duplicates()
    assert duplicates(conn) == {'chanA': None, 'chanB': None}


def test_erroring_channel_never_becomes_the_owner():
    """公开转私密后旧的用户名条目一直是 error：它 added 更早，但绝不能当正本。

    否则判重会跳过唯一能用的数字 ID 条目，把能工作的频道停掉。
    """
    conn = make_db([('dead', '同名频道', 1, '1001'), ('live', '同名频道', 2, '1001')])
    with conn:
        conn.execute("UPDATE channel_state SET state='error' WHERE name='dead'")
    engine = engine_on(conn, ['dead', 'live'])
    engine._mark_duplicates()
    assert duplicates(conn) == {'dead': 'live', 'live': None}


def test_channel_removed_from_config_does_not_own_the_duplicate():
    """从配置里删掉但 channel_state 还留着的行（游标要留着）不能参与判重。

    否则它会被当成正本，把用户新加的那条标成重复。
    """
    conn = make_db([('ltxjasna', 'Jasna LTX 频道', 1, '4419698318'), ('-1004419698318', 'Jasna LTX 新标题', 2, '4419698318')])
    engine = engine_on(conn, ['-1004419698318'])          # ltxjasna 已从配置里删掉
    engine._mark_duplicates()
    assert duplicates(conn) == {'ltxjasna': None, '-1004419698318': None}
    assert engine.store.events == []


def test_duplicate_notice_is_sent_only_once():
    """每轮抓取都会重算判重，但通知只能发一次——否则每轮刷屏（0.9.15 踩过同样的坑）。"""
    conn = make_db([('ltxjasna', 'Jasna LTX 频道', 1, '4419698318'), ('-1004419698318', 'Jasna LTX 新标题', 2, '4419698318')])
    engine = engine_on(conn, ['ltxjasna', '-1004419698318'])
    for _ in range(3):
        engine._mark_duplicates()
    assert len(engine.store.events) == 1


def test_duplicate_clears_when_peer_changes():
    conn = make_db([('chanA', '频道甲', 1, '1001'), ('chanB', '频道甲', 2, '1001')])
    engine = engine_on(conn, ['chanA', 'chanB'])
    engine._mark_duplicates()
    with conn:
        conn.execute("UPDATE channel_state SET peer_id='2002' WHERE name='chanB'")
    engine._mark_duplicates()
    assert duplicates(conn) == {'chanA': None, 'chanB': None}
    assert len(engine.store.events) == 1
