"""Rename only the local library namespace; preserve cloud identities and tokens."""
import json

OLD='StrimKeep媒体库'
NAME='影视媒体库'

def rewrite(value):
    if isinstance(value,str):
        if value.startswith(OLD+'/'):return NAME+value[len(OLD):]
        return value.replace('/'+OLD+'/', '/'+NAME+'/')
    if isinstance(value,list):return [rewrite(v) for v in value]
    if isinstance(value,dict):return {rewrite(k):rewrite(v) for k,v in value.items()}
    return value

def migrate(store,root):
    old,new=root/OLD,root/NAME
    state=store.meta('library_name_migration')
    if state=='done':return
    if old.is_symlink() or new.is_symlink():
        store.event('system','媒体库目录存在软链接，保留原目录；新输出使用影视媒体库。');return
    if old.exists() and new.exists():
        store.event('system','新旧媒体库目录同时存在，保留两边文件；全量同步按新目录核对，未强行覆盖。');return
    if not old.exists() and not (state=='started' and new.exists()):return
    # Check local ownership collisions before moving a single directory.
    owned=store.db.execute('SELECT * FROM owned_outputs').fetchall()
    for row in owned:
        relative=rewrite(row['relative_path'])
        if relative!=row['relative_path']:
            other=store.db.execute('SELECT account_id,file_id FROM owned_outputs WHERE relative_path=?',(relative,)).fetchone()
            if other and tuple(other)!=(row['account_id'],row['file_id']):
                store.event('system','媒体库新路径已有其他文件记录，保留旧目录，由后续同步逐项处理。');return
    with store.db:store.db.execute("INSERT OR REPLACE INTO meta VALUES('library_name_migration','started')")
    if old.exists():
        try:old.rename(new)
        except OSError:
            store.event('system','影视媒体库目录更名未完成，原文件保留；请检查本地目录写权限，不阻塞其他任务。');return
    pending={}
    with store.db:
        for row in store.db.execute('SELECT account_id,file_id,relative_path FROM output_registry').fetchall():
            if row['relative_path'].startswith(OLD+'/'):pending[row['account_id']+':'+row['file_id']]=row['relative_path']
        for table,columns in {'playbacks':['relative_path'],'organized_files':['relative_path'],'output_registry':['relative_path','files'],'owned_outputs':['relative_path'],'organize_items':['plan','original'],'organize_jobs':['config'],'emby_deliveries':['payload']}.items():
            for row in store.db.execute('SELECT rowid AS internal_row,* FROM '+table).fetchall():
                for column in columns:
                    raw=row[column]
                    if not isinstance(raw,str) or OLD not in raw:continue
                    if column in ('plan','original','config','files','payload'):
                        changed=json.dumps(rewrite(json.loads(raw)),ensure_ascii=False)
                    else:changed=rewrite(raw)
                    if changed!=raw:store.db.execute('UPDATE OR REPLACE '+table+' SET '+column+'=? WHERE rowid=?',(changed,row['internal_row']))
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('library_name_pending',?)",(json.dumps(pending,ensure_ascii=False),))
        store.db.execute('DELETE FROM sync_snapshots')
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('library_name_migration','done')")
    store.event('system','本地目录已更名为影视媒体库，文件及播放 Token 保留；Emby 若选了旧子目录，请改选新路径。')
