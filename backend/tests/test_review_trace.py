"""转人工复核必须留下可追溯的痕迹。

`organize_items` 明细会被 prune 按 job_days/keep_jobs 清掉，所以"为什么没识别出来"
只能靠事件表承载 —— 否则事后无法回答这个问题。
"""
import json

from test_organize import setup


def _events(store):
    return [r[0] for r in store.db.execute("SELECT message FROM events WHERE kind='organize' ORDER BY id")]


def _new_item(store, name):
    with store.db:
        cur = store.db.execute(
            "INSERT INTO organize_items(job_id,file_id,original,state,plan) VALUES(1,'f1',?, 'queued','{}')",
            (json.dumps({'name': name}),))
    return cur.lastrowid


def test_review_transition_emits_traceable_event(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    item_id = _new_item(store, '陌生发布组 2019 第03话.mkv')
    engine.update_item(item_id, state='review', step='识别失败', message='缺少季集号')
    messages = _events(store)
    assert len(messages) == 1
    assert '转人工复核' in messages[0]
    assert '陌生发布组 2019 第03话.mkv' in messages[0]      # 文件名要能看见
    assert '缺少季集号' in messages[0]                       # 原因要能看见
    store.close()


def test_non_review_update_emits_nothing(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    item_id = _new_item(store, '正常文件.mkv')
    engine.update_item(item_id, state='done', step='归档完成')
    assert _events(store) == []
    store.close()


def test_review_emit_failure_does_not_break_update(tmp_path):
    """留痕自身出错不能反过来把整理搞挂。"""
    store, cloud, http, engine, _ = setup(tmp_path)
    item_id = _new_item(store, 'x.mkv')

    def boom(*a, **k):
        raise RuntimeError('events 不可写')
    store.event = boom
    engine.update_item(item_id, state='review', step='识别失败', message='缺线索')
    assert store.db.execute("SELECT state FROM organize_items WHERE id=?", (item_id,)).fetchone()[0] == 'review'
    store.close()
