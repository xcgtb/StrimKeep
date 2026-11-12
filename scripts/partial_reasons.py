"""partial / needs_review 的失败分布：同一处反复失败，还是每次不同？

只读。用法（容器内）：python scripts/partial_reasons.py
结论用途：
- 若是**同一个目录**反复失败 → 长期坏点，重试策略无效，应升级为 needs_review 让人处理
- 若是**每次都不同** → 偶发，重试是合理的，只需把 partial 数字解释清楚
"""
import collections
import os
import re
import sqlite3

root = os.environ.get('STRIMKEEP_DATA_DIR', '/data')
db_path = None
for candidate in (os.path.join(root, 'data', 'state.db'), os.path.join(root, 'state.db')):
    if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
        db_path = candidate
        break
if db_path is None:
    raise SystemExit('找不到非空的 state.db')

db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
db.row_factory = sqlite3.Row

rows = db.execute("""SELECT id, state, message FROM organize_jobs
                     WHERE state IN ('partial','needs_review') AND message IS NOT NULL
                     ORDER BY id DESC LIMIT 300""").fetchall()

print(f'样本：最近 {len(rows)} 条 partial/needs_review 任务\n')

# 按「失败短句」归类：取中文短语前 12 字作为 key，避免同类文案因数字不同被拆散。
buckets = collections.Counter()
dirs = collections.Counter()
for row in rows:
    for clause in re.split(r'[；;。]', row['message'] or ''):
        clause = clause.strip()
        if not clause:
            continue
        key = re.sub(r'\d+', 'N', clause)[:28]
        buckets[key] += 1
        # 抓「N 个目录读取失败」前面可能带的路径
        for m in re.findall(r'(/[^\s，；。]{2,60})', clause):
            dirs[m] += 1

print('=== 一、失败短句分布（Top 12）===')
for key, n in buckets.most_common(12):
    print(f'  {n:>4}x  {key}')

print('\n=== 二、出现过的路径（Top 12，判断是否长期坏点）===')
if not dirs:
    print('  （消息里没有绝对路径——说明当前只报了数量，没报位置）')
for path, n in dirs.most_common(12):
    print(f'  {n:>4}x  {path}')

db.close()
