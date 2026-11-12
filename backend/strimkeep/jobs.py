"""任务状态机：`organize_jobs` / `organize_items` 的唯一写入入口（0.9.60）。

为什么要有这个模块
------------------
架构审计 A1 数出来：`organize_jobs` 有 **9 个写入方**、`organize_items` 有 **6 个**，
各自拼 SQL、各自决定能写哪一步。0.9.47 的「未完成当完成」不是某一行写错了，而是
**没有任何地方能拒绝一次错误的写入**：

- 状态靠手拼字符串，拼错（`completd`）照样入库，报表按已知词统计就永远看不见它；
- 任务可以从 `running` 直接跳 `completed`，没跑完的条目没人收口，却不再计入「未处理」；
- 任何写入方都能把终态改回中间态，事后翻不出是谁改的。

这里把它收敛成一张**迁移表**加一组受检写入。三条约束，全部 fail-closed：

1. 只认 `JOB_STATES` / `ITEM_STATES` 里的词，写别的抛 `JobStateError`；
2. 迁移必须符合 `JOB_TRANSITIONS` / `ITEM_TRANSITIONS`（同状态重复写＝幂等，放行）；
3. 任务进终态时**必须收口**：把还停在 `discovered` / `planned` 的条目落成 `skipped`
   并写清原因。这是「任务说完成了、其实还有文件没处理」的直接解药。

`tests/test_jobs_repo.py` 会扫描源码守住第 4 条：除本模块与 `store.py` 的建表/维护
SQL 外，任何文件直接 `UPDATE`/`INSERT INTO organize_jobs|organize_items` 都让门禁失败。
新增功能请在这里加入口，不要绕。
"""
import contextlib
import time

from .core import dumps

# ---------------------------------------------------------------- 状态词表
JOB_STATES = ('queued', 'running', 'paused', 'waiting_login', 'needs_review',
              'partial', 'completed', 'cancelled')
# 终态：任务不再自己往下走，只能人工重开（见 close/update）。
JOB_TERMINAL = frozenset({'partial', 'completed', 'cancelled'})
# 非终态之间允许的迁移。进入终态不设限制（收口本来就发生在任意中间态），
# 但**离开**终态只允许回到 queued，也就是显式重开。
JOB_TRANSITIONS = {
    'queued': {'running', 'paused', 'waiting_login', 'needs_review'},
    'running': {'queued', 'paused', 'waiting_login', 'needs_review'},
    'paused': {'queued', 'running', 'waiting_login', 'needs_review'},
    'waiting_login': {'queued', 'running', 'paused', 'needs_review'},
    'needs_review': {'queued', 'running'},
    # 终态回到运行态在真实链路里存在：本地输出补齐（repair_only）与人工重跑会把
    # 同一个任务重新跑起来。允许，但只能回 queued/running——不会绕过收口，因为
    # 「进入」终态时该收的已经收完了。
    'partial': {'queued', 'running'},
    'completed': {'queued', 'running'},
    'cancelled': {'queued', 'running'},
}
NOTIFY_STATES = frozenset({'not_started', 'checking', 'retrying', 'accepted', 'inflight',
                           'verified', 'unconfirmed', 'partial', 'unknown', 'not_needed'})

ITEM_STATES = ('queued', 'discovered', 'pending', 'planned', 'review', 'archived',
               'skipped', 'done', 'recycled')
ITEM_TERMINAL = frozenset({'done', 'recycled'})
ITEM_TRANSITIONS = {
    # queued 是老数据与测试夹具里的初始态（现场库里已经没有），只允许离开、不允许回去。
    'queued': {'discovered', 'pending', 'planned', 'review', 'skipped', 'archived', 'done', 'recycled'},
    'discovered': {'pending', 'planned', 'review', 'skipped', 'archived', 'done', 'recycled'},
    'pending': {'planned', 'review', 'skipped', 'archived', 'done', 'recycled'},
    'planned': {'pending', 'review', 'skipped', 'archived', 'done', 'recycled'},
    'review': {'pending', 'planned', 'skipped', 'archived', 'done', 'recycled'},
    'archived': {'review', 'skipped', 'done', 'recycled'},
    'skipped': {'pending', 'planned', 'review', 'archived', 'done', 'recycled'},
    'done': {'archived', 'review', 'skipped'},
    'recycled': {'archived', 'skipped'},
}
# 收口时只归一这两种「还没碰过云端」的状态，绝不覆盖真实结果。
SETTLE_STATES = ('discovered', 'planned')


class JobStateError(RuntimeError):
    """状态词或状态迁移不合法。故意抛异常：写错要炸在调用现场，不能静默入库。"""


class Jobs:
    """`organize_jobs` / `organize_items` 的受检写入层。

    `rejected` 计数进 /metrics（`strimkeep_job_rejected`）：它长期为 0 才说明所有写入方
    都按迁移表走；一旦上涨，说明有人绕过，或者真出现了非法迁移。
    """

    def __init__(self, store):
        self.store = store
        self.rejected = 0

    def _atomic(self):
        """写操作的事务边界。

        调用方用 `store.transaction()` 显式开了事务时并入它，否则自己提交一次。
        为什么必须这样：分享入队要「建任务 + 存密钥 + 认领频道」一起成功或一起失败，
        每条写各自提交会把前一半先落库（门禁 test_phase2 的
        `test_channel_job_and_claim_commit_together` 抓到过）。
        """
        if getattr(self.store, '_tx_depth', 0) > 0:
            return contextlib.nullcontext()
        return self.store.db

    # ------------------------------------------------------------- 读
    def get(self, job_id):
        return self.store.db.execute('SELECT * FROM organize_jobs WHERE id=?', (job_id,)).fetchone()

    def item(self, item_id):
        return self.store.db.execute('SELECT * FROM organize_items WHERE id=?', (item_id,)).fetchone()

    # ------------------------------------------------------------- 校验
    def _reject(self, message):
        self.rejected += 1
        raise JobStateError(message)

    def _job_state(self, state):
        if state not in JOB_STATES:
            self._reject('未知任务状态 %r（允许：%s）' % (state, ', '.join(JOB_STATES)))

    def _item_state(self, state):
        if state not in ITEM_STATES:
            self._reject('未知条目状态 %r（允许：%s）' % (state, ', '.join(ITEM_STATES)))

    def _check_job_transition(self, job_id, target):
        self._job_state(target)
        row = self.get(job_id)
        if row is None:
            self._reject('任务 %s 不存在，不能写状态 %r' % (job_id, target))
        current = row['state']
        if current == target or target in JOB_TERMINAL:
            return current
        if target not in JOB_TRANSITIONS.get(current, set()):
            self._reject('任务 %s 非法迁移 %s → %s' % (job_id, current, target))
        return current

    def _check_item_transition(self, item_id, target):
        self._item_state(target)
        row = self.item(item_id)
        if row is None:
            self._reject('条目 %s 不存在，不能写状态 %r' % (item_id, target))
        current = row['state']
        if current == target:
            return current
        if target not in ITEM_TRANSITIONS.get(current, set()):
            self._reject('条目 %s 非法迁移 %s → %s' % (item_id, current, target))
        return current

    # ------------------------------------------------------------- 写：任务
    def create(self, config, state='queued'):
        self._job_state(state)
        now = time.time()
        # config 既可能是 dict，也可能已经是 JSON 字符串（老调用点自己 dumps 过）：
        # 字符串原样入库，别再编码一次——双重编码会让 json.loads(row['config'])
        # 返回 str，下游 `config.get(...)` 直接炸。
        payload = config if isinstance(config, str) else dumps(config)
        with self._atomic():
            cursor = self.store.db.execute(
                'INSERT INTO organize_jobs(state,config,created,updated) VALUES(?,?,?,?)',
                (state, payload, now, now))
        return cursor.lastrowid

    def update(self, job_id, *, state=None, stage=None, message=None, config=None,
               notify_state=None, manifest=None):
        """受检更新。进终态时自动收口，返回 `settle()` 的结果。"""
        values = {}
        if state is not None:
            values['state'] = state
            self._check_job_transition(job_id, state)
        if notify_state is not None:
            if notify_state not in NOTIFY_STATES:
                self._reject('未知通知状态 %r' % (notify_state,))
            values['notify_state'] = notify_state
        if stage is not None:
            values['stage'] = stage
        if message is not None:
            values['message'] = message
        if config is not None:
            values['config'] = config if isinstance(config, str) else dumps(config)
        if manifest is not None:
            values['manifest'] = 1 if manifest else 0
        if not values:
            return {}
        values['updated'] = time.time()
        with self._atomic():
            self.store.db.execute(
                'UPDATE organize_jobs SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
                (*values.values(), job_id))
        if values.get('state') in JOB_TERMINAL:
            return self.settle(job_id)
        return {}

    def finish(self, job_id, state, stage=None, message=None):
        """收口。state 必须是终态：把「任务结束」这件事钉在一个函数上。"""
        if state not in JOB_TERMINAL:
            self._reject('finish() 只接受终态，收到 %r' % (state,))
        return self.update(job_id, state=state, stage=stage, message=message)

    def close(self, job_id, message=''):
        """人工结项：部分未完成 / 待核查 / 已取消 → 已完成。"""
        row = self.get(job_id)
        if row is None:
            self._reject('任务 %s 不存在' % (job_id,))
        if row['state'] not in ('partial', 'needs_review', 'cancelled'):
            self._reject('任务 %s 处于 %s，不能人工结项' % (job_id, row['state']))
        return self.update(job_id, state='completed', message=message, stage='人工结项')

    def recover_running(self, stage='重启后核查恢复'):
        """进程重启：把停在 running 的任务放回 queued，等 worker 重新核查。"""
        with self._atomic():
            count = self.store.db.execute(
                'UPDATE organize_jobs SET state=?,stage=?,updated=? WHERE state=?',
                ('queued', stage, time.time(), 'running')).rowcount
            self.store.db.execute(
                'UPDATE organize_jobs SET notify_state=? WHERE notify_state=?',
                ('unknown', 'inflight'))
        return count

    def set_config(self, job_id, config):
        return self.update(job_id, config=config)

    def set_notify_state(self, job_id, notify_state):
        return self.update(job_id, notify_state=notify_state)

    def set_manifest(self, job_id, manifest=1):
        return self.update(job_id, manifest=manifest)

    # ------------------------------------------------------------- 写：条目
    def update_item(self, item_id, *, state=None, step=None, message=None, plan=None,
                    original=None):
        values = {}
        if state is not None:
            values['state'] = state
            self._check_item_transition(item_id, state)
        if step is not None:
            values['step'] = step
        if message is not None:
            values['message'] = message
        if plan is not None:
            values['plan'] = plan if isinstance(plan, str) else dumps(plan)
        if original is not None:
            values['original'] = original if isinstance(original, str) else dumps(original)
        if not values:
            return False
        with self._atomic():
            self.store.db.execute(
                'UPDATE organize_items SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
                (*values.values(), item_id))
        return True

    def set_item_plan(self, item_id, plan):
        return self.update_item(item_id, plan=plan)

    def replace_manifest(self, job_id, rows):
        """整表重建：先删后插（rows 是 (file_id, original) 序列）。"""
        with self._atomic():
            self.store.db.execute('DELETE FROM organize_items WHERE job_id=?', (job_id,))
            self.store.db.executemany(
                'INSERT INTO organize_items(job_id,file_id,original) VALUES(?,?,?)',
                [(job_id, file_id, original if isinstance(original, str) else dumps(original))
                 for file_id, original in rows])
            self.store.db.execute('UPDATE organize_jobs SET manifest=1 WHERE id=?', (job_id,))

    def clear_items(self, job_id):
        """清空任务清单行（重建前调用）。"""
        with self._atomic():
            return self.store.db.execute('DELETE FROM organize_items WHERE job_id=?', (job_id,)).rowcount

    def insert_item(self, job_id, file_id, original, plan='{}', state='discovered'):
        """逐条追加清单行（分享产线要按分享记录单独算 identity 与初始状态）。"""
        self._item_state(state)
        with self._atomic():
            cursor = self.store.db.execute(
                'INSERT INTO organize_items(job_id,file_id,original,plan,state) VALUES(?,?,?,?,?)',
                (job_id, file_id,
                 original if isinstance(original, str) else dumps(original),
                 plan if isinstance(plan, str) else dumps(plan), state))
        return cursor.lastrowid

    def copy_manifest(self, source_job, target_job):
        with self._atomic():
            self.store.db.execute(
                'INSERT INTO organize_items(job_id,file_id,original) '
                'SELECT ?,file_id,original FROM organize_items WHERE job_id=?',
                (target_job, source_job))
            self.store.db.execute('UPDATE organize_jobs SET manifest=1 WHERE id=?', (target_job,))

    def prune_manifest(self, job_id, keep_file_ids):
        """按 file_id 白名单清掉这一批没扫到的条目。"""
        marks = ','.join('?' for _ in keep_file_ids)
        with self._atomic():
            return self.store.db.execute(
                'DELETE FROM organize_items WHERE job_id=? AND file_id NOT IN (%s)' % marks,
                (job_id, *keep_file_ids)).rowcount

    def clear_publication_flags(self, job_id):
        """重启后清掉内存队列标志（只对 archived 行生效）。"""
        with self._atomic():
            return self.store.db.execute(
                "UPDATE organize_items SET plan=json_remove(plan,'$.queued') "
                "WHERE job_id=? AND state='archived'", (job_id,)).rowcount

    def mark_discovered_as_review(self, job_id, step, message):
        """任务卡在识别阶段：discovered 一起落 review，别让详情页显示「发现文件」。"""
        with self._atomic():
            return self.store.db.execute(
                "UPDATE organize_items SET state='review', step=?, message=? "
                "WHERE job_id=? AND state='discovered'", (step, message, job_id)).rowcount

    def settle(self, job_id, message='任务结束未处理'):
        """终态收口：归一没开始处理的条目，并释放该任务的转存密钥。

        原实现是 `Store.settle_terminal_job`；搬到这里是为了让「进终态」和「收口」
        在同一个模块里，没有第二条路可以只改状态不收口。
        """
        from .store import TRANSFER_SECRET
        marks = ','.join('?' for _ in SETTLE_STATES)
        with self._atomic():
            settled = self.store.db.execute(
                "UPDATE organize_items SET state='skipped', step='任务结束未处理', message=? "
                "WHERE job_id=? AND state IN (%s)" % marks,
                (message, job_id, *SETTLE_STATES)).rowcount
            released = self.store.db.execute('DELETE FROM settings WHERE key=?',
                                             (TRANSFER_SECRET + str(job_id),)).rowcount
        return {'items': max(0, settled), 'secrets': max(0, released)}

    # ------------------------------------------------------------- 批量收尾
    def skip_job_items(self, job_id, step, message=None):
        """把任务下还没处理的条目落 skipped（人工结项用；message 不传就保留原值）。"""
        sql = "UPDATE organize_items SET state='skipped', step=?"
        params = [step]
        if message is not None:
            sql += ', message=?'
            params.append(message)
        sql += " WHERE job_id=? AND state IN ('review','pending')"
        params.append(job_id)
        with self._atomic():
            return self.store.db.execute(sql, tuple(params)).rowcount

    def skip_reconciled(self, item_id, reason, note):
        """账本对账：文件已归档/已移走，条目落 skipped 并追加原因（保留原说明）。"""
        with self._atomic():
            return self.store.db.execute(
                "UPDATE organize_items SET state='skipped', step=?,"
                " message=CASE WHEN message='' THEN ?"
                " WHEN message LIKE ? THEN message ELSE message || '；' || ? END"
                " WHERE id=? AND state IN ('review','pending')",
                (reason, note, '%' + note + '%', note, item_id)).rowcount

    def skip_recovered(self, item_id):
        """同一文件已由后续批次归档：旧条目落 skipped，说明按固定文案合并。"""
        with self._atomic():
            return self.store.db.execute(
                "UPDATE organize_items SET state='skipped', step=?,"
                " message=CASE WHEN message LIKE '%同一文件在后续批次已成功归档%' THEN message"
                " WHEN message='' THEN ? ELSE message || '；' || ? END WHERE id=?",
                ('已由后续批次补齐', '同一文件在后续批次已成功归档。',
                 '同一文件在后续批次已成功归档。', item_id)).rowcount

    def note_superseded(self, old_job_id, file_ids, step, message):
        """后续批次已补齐同一文件：给旧任务对应条目补说明（不改状态）。"""
        if not file_ids:
            return 0
        marks = ','.join('?' for _ in file_ids)
        with self._atomic():
            return self.store.db.execute(
                "UPDATE organize_items SET step=?, message=CASE WHEN message='' THEN ? "
                "ELSE message || '；' || ? END WHERE job_id=? AND file_id IN (%s) "
                "AND (state IN ('pending','review') OR (state='skipped' AND step LIKE '%%未识别%%'))"
                % marks,
                (step, message, message, old_job_id, *file_ids)).rowcount


def install(store):
    """给 Store 挂一个 `jobs` 属性，各处用 `store.jobs.xxx`。"""
    if getattr(store, 'jobs', None) is None:
        store.jobs = Jobs(store)
    return store.jobs



