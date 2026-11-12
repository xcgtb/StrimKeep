"""未识别视频（转人工复核）的原因分布。

只读。用法（容器内）：python scripts/review_reasons.py
数据来源：organize_items 里 state='review' 的条目——它们就是任务消息里
「待处理 N 个视频」的来源。找出集中在哪几类原因上，才知道该提升识别能力，
还是该改重试/分类策略。
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

rows = db.execute("""SELECT step, message, original FROM organize_items
                     WHERE state='review' ORDER BY id DESC LIMIT 800""").fetchall()
print(f'样本：最近 {len(rows)} 条 state=review 的条目\n')

reasons = collections.Counter()
steps = collections.Counter()
exts = collections.Counter()
for row in rows:
    msg = (row['message'] or '').strip()
    step = (row['step'] or '').strip()
    steps[step] += 1
    reasons[re.sub(r'\d+', 'N', msg)[:44]] += 1
    try:
        name = str(__import__('json').loads(row['original'] or '{}').get('name') or '')
    except Exception:
        name = ''
    if '.' in name:
        exts[name.rsplit('.', 1)[-1].lower()[:8]] += 1

print('=== 一、原因分布（Top 12）===')
for key, n in reasons.most_common(12):
    print(f'  {n:>4}x  {key or "(空)"}')

print('\n=== 二、所处步骤（Top 8）===')
for key, n in steps.most_common(8):
    print(f'  {n:>4}x  {key or "(空)"}')

print('\n=== 三、文件扩展名（Top 8）===')
for key, n in exts.most_common(8):
    print(f'  {n:>4}x  .{key}')

db.close()
