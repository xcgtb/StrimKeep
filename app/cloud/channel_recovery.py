"""Bounded resumable public-page history and explicit, previewed cursor resets."""
import json
import time
from app.cloud import store, credentials, channel_network
from app.cloud.errors import CloudError

MAX_STAGED = 1000
MAX_BYTES = 2 * 1024 * 1024


def load(channel_id, account_revision, rule_revision, cursor):
    with store.db() as con:
        row = con.execute('SELECT account_revision,rule_revision,cursor,tip,before_id,sealed,count FROM cloud_channel_backfill WHERE channel_id=?', (channel_id,)).fetchone()
    if not row or row[:3] != (account_revision, rule_revision, cursor):
        return None
    return {'tip': row[3], 'before': row[4], 'sealed': row[5], 'messages': credentials.unseal(row[5])}


def matches(con, channel_id, previous):
    row = con.execute('SELECT sealed FROM cloud_channel_backfill WHERE channel_id=?', (channel_id,)).fetchone()
    return (row[0] if row else None) == (previous['sealed'] if previous else None)


def stage(channel_id, account_id, arev, rev, cursor, previous, partial):
    combined = {m['message_id']: m for m in (previous['messages'] if previous else []) + partial['messages']}
    rows = sorted(combined.values(), key=lambda m: m['message_id'])
    if len(rows) > MAX_STAGED or len(store.encode(rows).encode()) > MAX_BYTES:
        raise CloudError('频道历史积压超过补读上限，请暂停后检查，再选择重新建立读取位置或使用用户会话', 409)
    sealed = credentials.seal(rows)
    tip = max(partial['tip'], previous['tip'] if previous else cursor)
    now = time.time()
    with store.db() as con:
        current = con.execute('SELECT c.revision,a.revision,COALESCE(s.cursor,0),c.payload FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE c.id=? AND c.account_id=?', (channel_id, account_id)).fetchone()
        if not current or current[:3] != (rev, arev, cursor) or not json.loads(current[3]).get('enabled'):
            return False
        old = con.execute('SELECT account_revision,rule_revision,cursor FROM cloud_channel_backfill WHERE channel_id=?', (channel_id,)).fetchone()
        if old and old != (arev, rev, cursor):
            con.execute('DELETE FROM cloud_channel_backfill WHERE channel_id=?', (channel_id,))
        if not matches(con, channel_id, previous):
            return False
        con.execute('INSERT OR REPLACE INTO cloud_channel_backfill VALUES(?,?,?,?,?,?,?,?,?)',
                    (channel_id, arev, rev, cursor, tip, partial['before'], sealed, len(rows), now))
        con.execute("INSERT INTO cloud_channel_cursors VALUES(?,?,1,?,'') ON CONFLICT(id) DO UPDATE SET next_poll=excluded.next_poll,last_error=''", (channel_id, cursor, now+2))
        con.execute('INSERT INTO cloud_public_channel_status VALUES(?,0,?,0,0) ON CONFLICT(channel_id) DO UPDATE SET last_check=excluded.last_check,failures=0', (channel_id, now))
    return True


@channel_network.probe
def preview(account_id, channel_id):
    from app.cloud import receiving, public_channel_reader
    rule = next((r for r in receiving.channels(account_id) if r['id'] == channel_id), None)
    account = store.account(account_id)
    if not rule or not account:
        raise CloudError('频道或账号不存在', 404)
    if rule['mode'] != 'public_web' or rule['enabled']:
        raise CloudError('请先暂停公开网页监控，再检查要略过的历史范围', 409)
    _, tip = public_channel_reader.fetch_messages(rule['address'].lstrip('@'), history_limit=0)
    with store.db() as con:
        row = con.execute('SELECT cursor FROM cloud_channel_cursors WHERE id=?', (channel_id,)).fetchone()
    cursor = row[0] if row else 0
    value = {'account_id': account_id, 'account_revision': account['revision'], 'channel_id': channel_id,
             'revision': rule['revision'], 'cursor': cursor, 'tip': max(tip, cursor), 'expires': time.time()+300}
    return {'token': credentials.seal(value), 'previous_cursor': cursor, 'latest_cursor': value['tip'],
            'message': '确认后将略过旧位置至本次最新位置之间尚未接收的消息；监控保持暂停，不会自动执行资源处理。'}


def apply(account_id, channel_id, token, confirmed):
    if confirmed is not True or not isinstance(token, str) or len(token) > 4096:
        raise CloudError('请先预览并明确确认略过历史消息')
    value = credentials.unseal(token)
    if not isinstance(value, dict) or value.get('account_id') != account_id or value.get('channel_id') != channel_id or value.get('expires', 0) < time.time():
        raise CloudError('读取位置预览已失效，请重新检查', 409)
    with store.db() as con:
        row = con.execute('SELECT c.revision,a.revision,c.payload,COALESCE(s.cursor,0) FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE c.id=? AND c.account_id=?', (channel_id, account_id)).fetchone()
        if not row or row[:2] != (value['revision'], value['account_revision']) or row[3] != value['cursor'] or json.loads(row[2]).get('enabled') or json.loads(row[2]).get('mode') != 'public_web':
            raise CloudError('频道、账号或读取位置已变化，请重新预览', 409)
        con.execute("INSERT INTO cloud_channel_cursors VALUES(?,?,1,0,'') ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,initialized=1,next_poll=0,last_error=''", (channel_id, value['tip']))
        con.execute('DELETE FROM cloud_channel_backfill WHERE channel_id=?', (channel_id,))
        con.execute('DELETE FROM cloud_public_channel_status WHERE channel_id=?', (channel_id,))
        con.execute('UPDATE cloud_channels SET revision=revision+1,updated=? WHERE id=?', (time.time(), channel_id))
        from app.cloud import events
        events.emit('channel', '用户确认重新建立频道读取位置：'+str(value['cursor'])+' → '+str(value['tip']), con=con)
    return {'message': '读取位置已更新，频道仍暂停；启用后只读取此位置之后的新消息'}
