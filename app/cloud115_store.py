"""Strict SQLite repository for account-bound 115 files and deletion receipts."""
import json
import time
import uuid
if __package__:
    from . import storage
else:
    import storage


def account(account_id):
    with storage._transaction() as con:
        row = con.execute('SELECT id,uid,name,access_token,refresh_token,expires_at,enabled,revision '
                          'FROM cloud115_accounts WHERE id=?', (account_id,)).fetchone()
    return dict(zip(('id','uid','name','access_token','refresh_token','expires_at','enabled','revision'), row)) if row else None


def accounts():
    with storage._transaction() as con:
        rows = con.execute('SELECT id,uid,name,enabled,updated_at FROM cloud115_accounts ORDER BY updated_at DESC').fetchall()
    return [dict(zip(('id','uid','name','enabled','updated_at'), row)) for row in rows]


def save_account(uid, name, access_token, refresh_token, expires_at):
    with storage._transaction() as con:
        row = con.execute('SELECT id FROM cloud115_accounts WHERE uid=?', (uid,)).fetchone()
        key = row[0] if row else uuid.uuid4().hex
        con.execute('INSERT INTO cloud115_accounts(id,uid,name,access_token,refresh_token,expires_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name, '
                    'access_token=excluded.access_token,refresh_token=excluded.refresh_token, '
                    'expires_at=excluded.expires_at,enabled=1,revision=cloud115_accounts.revision+1,updated_at=excluded.updated_at',
                    (key,uid,name,access_token,refresh_token,expires_at,time.time()))
    return key


def rotate(account_id, revision, access_token, refresh_token, expires_at):
    with storage._transaction() as con:
        cur = con.execute('UPDATE cloud115_accounts SET access_token=?,refresh_token=?,expires_at=?,enabled=1, '
                          'revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                          (access_token,refresh_token,expires_at,time.time(),account_id,revision))
        if cur.rowcount != 1:
            raise ValueError('115 授权已变化，请重新检查连接')


def disable_account(account_id):
    with storage._transaction() as con:
        con.execute('UPDATE cloud115_accounts SET enabled=0,updated_at=? WHERE id=?', (time.time(),account_id))


def binding(*, path=None, binding_id=None):
    with storage._transaction() as con:
        key, value = ('strm_path',str(path)) if path is not None else ('id',binding_id)
        cur = con.execute('SELECT * FROM cloud115_bindings WHERE '+key+'=? '
                          "ORDER BY CASE WHEN state='deleted' THEN 1 ELSE 0 END, updated_at DESC LIMIT 1", (value,))
        row = cur.fetchone()
        result = dict(zip((d[0] for d in cur.description),row)) if row else None
    if result:
        for field in ('remote','sidecars','fingerprint'):
            result[field] = json.loads(result[field])
    return result


def reserve(account_id, remote, path, root, token_hash, sidecars):
    key, now = uuid.uuid4().hex, time.time()
    with storage._transaction() as con:
        # Never rebind a path/file whose remote mutation may already be running.
        con.execute('INSERT INTO cloud115_bindings '
                    '(id,account_id,file_id,strm_path,root,remote,sidecars,token_hash,state,created_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (key,account_id,remote['id'],str(path),str(root),json.dumps(remote),
                     json.dumps(sidecars),token_hash,'generating',now,now))
        con.executemany('INSERT INTO cloud115_binding_files(account_id,file_id,binding_id) VALUES(?,?,?)',
                        [(account_id,x['id'],key) for x in [remote]+sidecars])
    return key


def file_owner(account_id,file_id):
    with storage._transaction() as con:
        row=con.execute('SELECT binding_id FROM cloud115_binding_files WHERE account_id=? AND file_id=?',
                        (account_id,file_id)).fetchone()
    return row[0] if row else None


def update_binding_locations(binding_id,files):
    """Name/parent updates only; immutable content and ID evidence remain."""
    with storage._transaction() as con:
        row=con.execute('SELECT remote,sidecars,state FROM cloud115_bindings WHERE id=?',(binding_id,)).fetchone()
        if not row or row[2]!='active':raise ValueError('115 绑定状态已变化')
        remote=json.loads(row[0]);sidecars=json.loads(row[1])
        for old in [remote]+sidecars:
            new=files.get(old['id'])
            if new:
                old['name']=new['name'];old['parent_id']=new['parent_id']
        con.execute('UPDATE cloud115_bindings SET remote=?,sidecars=?,updated_at=? WHERE id=?',
                    (json.dumps(remote),json.dumps(sidecars),time.time(),binding_id))


def received(account_id,file_id):
    with storage._transaction() as con:
        cur=con.execute('SELECT * FROM cloud115_received_files WHERE account_id=? AND file_id=?',(account_id,file_id))
        row=cur.fetchone();result=dict(zip((x[0] for x in cur.description),row)) if row else None
    if result:
        result['remote']=json.loads(result['remote']);result['related_ids']=json.loads(result['related_ids'])
    return result


def received_source(account_id,share_code,source_file_id):
    with storage._transaction() as con:
        rows=con.execute('SELECT file_id,remote FROM cloud115_received_files WHERE account_id=? AND share_code=? AND source_file_id=?',
                         (account_id,share_code,source_file_id)).fetchall()
    return [{'file_id':r[0],'remote':json.loads(r[1])} for r in rows]


def save_received(plan):
    targets=plan['targets'];related=[x['file']['id'] for x in targets[1:]]
    with storage._transaction() as con:
        for i,item in enumerate(targets):
            fid=item['file']['id']
            old=con.execute('SELECT transfer_id,source_file_id FROM cloud115_received_files WHERE account_id=? AND file_id=?',
                            (plan['account_id'],fid)).fetchone()
            if old and old!=(plan['id'],item['source_id']):raise ValueError('转存目标 ID 已有不同的来源归属')
            con.execute('INSERT OR IGNORE INTO cloud115_received_files(account_id,file_id,transfer_id,share_code,source_file_id,role,remote,related_ids,created_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?)',(plan['account_id'],fid,plan['id'],plan['parameters']['share_code'],item['source_id'],
                        'video' if i==0 else 'metadata',json.dumps(item['file']),json.dumps(related if i==0 else []),time.time()))


def set_binding_state(key, state, fingerprint=None):
    with storage._transaction() as con:
        if fingerprint is None:
            con.execute('UPDATE cloud115_bindings SET state=?,updated_at=? WHERE id=?', (state,time.time(),key))
        else:
            con.execute('UPDATE cloud115_bindings SET state=?,fingerprint=?,updated_at=? WHERE id=?',
                        (state,json.dumps(fingerprint),time.time(),key))


def bindings(limit=50, offset=0):
    with storage._transaction() as con:
        rows = con.execute('SELECT id,account_id,file_id,strm_path,state,created_at,updated_at '
                           'FROM cloud115_bindings ORDER BY created_at DESC LIMIT ? OFFSET ?', (limit,offset)).fetchall()
    return [dict(zip(('id','account_id','file_id','strm_path','state','created_at','updated_at'),row)) for row in rows]


def receipt(binding_id, file_id):
    with storage._transaction() as con:
        row = con.execute('SELECT expected,state,message FROM cloud115_deletions WHERE binding_id=? AND file_id=?',
                          (binding_id,file_id)).fetchone()
    return {'expected':json.loads(row[0]),'state':row[1],'message':row[2]} if row else None


def begin_delete(binding_id, expected):
    with storage._transaction() as con:
        cur = con.execute('INSERT OR IGNORE INTO cloud115_deletions(binding_id,file_id,expected,state,updated_at) '
                          'VALUES(?,?,?,?,?)', (binding_id,expected['id'],json.dumps(expected),'submitted',time.time()))
    return cur.rowcount == 1


def finish_delete(binding_id, file_id, state, message=''):
    with storage._transaction() as con:
        con.execute('UPDATE cloud115_deletions SET state=?,message=?,updated_at=? WHERE binding_id=? AND file_id=?',
                    (state,message,time.time(),binding_id,file_id))


def deletions(limit=50):
    with storage._transaction() as con:
        rows = con.execute('SELECT binding_id,file_id,state,message,updated_at FROM cloud115_deletions '
                           'ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
    return [dict(zip(('binding_id','file_id','state','message','updated_at'),row)) for row in rows]
