"""真实使用审计：直接读线上库，看任务实际跑成什么样。

只读，不写任何东西。目的不是验证"没坏"，而是回答几个产品问题：
- 有多少任务停在 needs_review / partial / failed，为什么？
- 事件表里哪类噪音最多，重要信息会不会被淹掉？
- 转存/归档/清理的实际耗时与失败率。
"""
import json
import os
import sqlite3
import time

DB = None
root = os.environ.get('STRIMKEEP_DATA_DIR', '/data')
for candidate in (os.path.join(root, 'data', 'state.db'), os.path.join(root, 'state.db')):
    if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
        DB = candidate
        break
if DB is None:
    raise SystemExit(f'找不到非空的 state.db（已查 {root} 及其 data/ 子目录）')
print(f'库文件: {DB}  ({os.path.getsize(DB)} bytes)')

db = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
db.row_factory = sqlite3.Row

def q(sql, *args):
    return db.execute(sql, args).fetchall()

now = time.time()
day = 86400

print('=== 一、任务状态分布（近 7 天）===')
for row in q("SELECT state, count(*) n FROM organize_jobs WHERE created>? GROUP BY state ORDER BY n DESC", now - 7 * day):
    print(f'  {row["state"]:<14} {row["n"]}')

print('\n=== 二、非完成任务的真实原因（最近 8 条）===')
rows = q("""SELECT id, json_extract(config,'$.job_kind') kind, state,
                   substr(coalesce(message,''),1,90) msg, updated
            FROM organize_jobs WHERE state NOT IN ('completed') AND created>?
            ORDER BY updated DESC LIMIT 8""", now - 30 * day)
if not rows:
    print('  （没有非完成任务）')
for r in rows:
    when = time.strftime('%m-%d %H:%M', time.localtime(r['updated']))
    print(f'  [{when}] #{r["id"]} {r["kind"] or "?"} → {r["state"]}: {r["msg"]}')

print('\n=== 三、事件类型分布（近 7 天）===')
for row in q("SELECT kind, count(*) n FROM events WHERE at>? GROUP BY kind ORDER BY n DESC", now - 7 * day):
    print(f'  {row["kind"]:<12} {row["n"]}')

print('\n=== 四、Top 事件消息（看噪音与可读性）===')
for row in q("""SELECT kind, substr(message,1,80) m, count(*) n FROM events WHERE at>?
                GROUP BY kind, m ORDER BY n DESC LIMIT 10""", now - 7 * day):
    print(f'  {row["n"]:>4}x [{row["kind"]}] {row["m"]}')

print('\n=== 五、台账规模 ===')
for table in ('organized_files', 'owned_outputs', 'organize_items', 'events'):
    try:
        n = q(f'SELECT count(*) c FROM {table}')[0]['c']
    except Exception as exc:
        n = f'查询失败: {exc}'
    print(f'  {table:<18} {n}')

print('\n=== 六、schema 版本 ===')
for row in q("SELECT key, value FROM meta WHERE key LIKE '%schema_version%' ORDER BY key"):
    print(f'  {row["key"]:<24} {row["value"]}')

db.close()
