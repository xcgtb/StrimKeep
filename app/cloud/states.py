# -*- coding: utf-8 -*-
"""业务任务的唯一状态词表。

分工（与 app/tasks.py 严格区分）：

    app/cloud/states.py  ——  业务任务状态（cloud_jobs.state），落库、跨重启存在
    app/tasks.py         ——  执行槽状态（EXEC_STATUSES），内存、仅表示占位

任何要展示给用户的进度、任何要落库的结局，都用本模块的词。
执行槽的 running/success/error/cancelled **不要**出现在业务页面上。
"""

# cloud_jobs.state 的取值。新增状态时先改这里，再改写入方。
JOB_STATES = frozenset({
    'queued',        # 已入队，等调度
    'running',       # 正在执行
    'success',       # 完成且无遗留
    'stopped',       # 在安全边界停止
    'failed',        # 明确失败
    'uncertain',     # 写操作结果未知——永不自动重放
    'needs_review',  # 有错误项，等人处理
    'cancelled',     # 历史值，仅用于展示旧记录
})

# 历史遗留值：仍可能出现在旧库的记录里，不要用于新写入。
LEGACY_JOB_STATES = frozenset({'cancelled'})

# 尚未结束的状态：用于"这个任务还在不在跑"的判断。
ACTIVE_JOB_STATES = frozenset({'queued', 'running'})

# 终态：不会再自己变化，只有人工操作能改。
TERMINAL_JOB_STATES = frozenset({'success', 'stopped', 'failed', 'uncertain', 'needs_review', 'cancelled'})

# 展示用文案。只做展示，不参与判断。
JOB_STATE_LABELS = {
    'queued': '等待执行',
    'running': '执行中',
    'success': '已完成',
    'stopped': '已安全停止',
    'failed': '执行失败',
    'uncertain': '结果待核实',
    'needs_review': '需要处理',
    'cancelled': '已取消',
}
