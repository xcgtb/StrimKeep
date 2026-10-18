# 加一个功能：七步

> 配套 `docs/ARCHITECTURE.md`。每次新增能力照此走，不要凭感觉挑落点。
> 目标：任何一次改动，你都能在 30 秒内说出它落在哪一层、为什么不落在别处。

---

## 先做一次判断（30 秒）

```
这个功能会改远端数据或本地文件吗？
├─ 会 ──> 重任务：走 jobs（持久、可停止、有回执）      → 步骤 1-5 全走
└─ 不会 ─> 轻任务：走 spawn 或直接调用
          ├─ 要遍历双库/耗时长 ──> deps.spawn（互斥）
          └─ 只读一次/很快     ──> 直接调用

它的核心是"判断"还是"流程"？
├─ 判断（怎么算重复、怎么算更好、该不该删）──> app/domain/，零 IO
└─ 流程（先查什么、再调谁、失败怎么办）   ──> app/cloud/ 或根目录业务模块
```

**判错的典型症状**：规则写进了路由 → 无法单测；流程写进了 domain → domain 开始 import requests。

---

## 第 1 步 · 规则进 `app/domain/`

要求：**函数签名里不出现** `con`、`requests`、`Path`、`engine`、`time.time()`。

```python
# app/domain/quality.py
def better_than(candidate: dict, current: dict) -> bool:
    """纯函数：输入两个描述符，输出该不该替换。"""
```

**为什么先写这里**：这是唯一能脱离环境单测的地方。规则一旦可测，"改一处崩一片"就止住了。

验收：`app/domain/` 的占比应当持续上升（当前 534 行 / 28,743 行 ≈ 1.9%，目标 15%）。

---

## 第 2 步 · 表与约束进 `app/cloud/store.py`

```python
CREATE TABLE IF NOT EXISTS cloud_xxx (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  ...
  UNIQUE(account_id, source_key)   -- ← 幂等键，写表之前就要定
);
```

三条要求：

1. **先定幂等键，再写表。** 唯一约束是所有重复写入问题的唯一便宜解
2. 用 `INSERT OR IGNORE` / `INSERT OR REPLACE`，不要"先 SELECT 再 INSERT"（有竞态）
3. 新表加在 `SCHEMA` 里即可，`db()` 每次事务自动建表；已有表结构变更需写迁移

**幂等键怎么选**：用业务键，不要用自增或时间。
参照 `cloud_job_due` 的 `UNIQUE(origin_job, file_id)` —— 不下载、不算 hash 就能判断"这件事做过没有"。

---

## 第 3 步 · 编排进 `app/cloud/<域>.py`

重任务一律用这套 API：

```python
from app.cloud import jobs

def start_xxx(account_id, items, source='web'):
    job_id = jobs.enqueue('xxx', {'account_id': account_id, 'items': items})
    return job_id

def _run(job_id, payload):
    total = len(payload['items'])
    for i, item in enumerate(payload['items'], 1):
        jobs.checkpoint(job_id)                      # ← 每个可能耗时的点前都要有
        jobs.step(job_id, f'item:{item["id"]}', item, lambda: do_one(item))
        jobs.progress(job_id, total, i, 'transfer', item.get('name', ''))
```

四条纪律：

| 纪律 | 为什么 |
|---|---|
| 每个循环体开头 `jobs.checkpoint()` | 没有它，停止请求要等到整批跑完才生效 |
| 每个独立动作包成 `jobs.step(...)` | 步骤级状态让"卡在哪一步"可查，也让重试有粒度 |
| `jobs.progress()` 只在状态变化时调 | 每文件都写会打爆 SQLite 写锁 |
| 写结果未知时抛 `uncertain`，不要吞掉 | 见第 4 步 |

---

## 第 4 步 · 失败分类（这一步决定系统的可信度）

三种失败必须区分，**不要都扔进 `failed`**：

| 分类 | 含义 | 处置 |
|---|---|---|
| 可重试 | 网络抖动、远端 5xx、限流 | 退避后自动重试，记 `attempts` |
| 不可重试 | 文件不存在、链接失效、参数非法 | 直接 `failed`，写清原因 |
| **结果未知** | 请求发出去了，但没拿到响应 | **`uncertain`，永不自动重放** |

第三条是整个系统最重要的一条纪律。`app/cloud/jobs.py` 开头就写着：

> `"""Durable queue backed by the existing task manager; never replay uncertain writes."""`

写操作不可重放的代价，远高于停下来让人看一眼。

---

## 第 5 步 · 路由进 `app/routers/`

路由文件只做三件事：鉴权、校验、派发。

```python
from fastapi import APIRouter, Depends
from app.routers.deps import auth
from app.cloud import xxx

router = APIRouter()

@router.post('/api/xxx/run')
def run(body: dict, _=Depends(auth)):
    return {'job_id': xxx.start_xxx(body['account_id'], body['items'])}
```

配套查询端点（前端靠它轮询）：

```python
@router.get('/api/xxx/status')
def status(job_id: str, _=Depends(auth)):
    return jobs.details(job_id)        # 已含 state / progress / steps / stop_requested
```

**禁止**在路由里出现 `con.execute`、`requests.get`、`Path(...)`。出现了说明落点错了。

轻任务用 `deps.spawn`（互斥执行槽）：

```python
from app.routers.deps import spawn
spawn('scan', do_scan, arg1, arg2)     # 已有任务在跑会抛 429
```

---

## 第 6 步 · 前端接上

| 改什么 | 放哪 |
|---|---|
| 请求函数 | `frontend/src/api/domain.ts` 或 `media.ts`（统一走 `client.ts`） |
| 共享状态 | `frontend/src/stores/`（Pinia：`cloud` / `tasks` / `ui` …） |
| 轮询进度 | 复用 `composables/useVisiblePoll.ts`，别自己写 `setInterval` |
| 一次操作的生命周期 | 复用 `composables/useOperation.ts` |
| 页面 | `frontend/src/views/XxxView.vue` |

**展示任务状态时只有一个来源：`cloud_jobs.state`**，取值与文案统一从 `app/cloud/states.py` 取（`JOB_STATES` / `JOB_STATE_LABELS`）。

不要拿 `app/tasks.py` 的 `ExecSlot`（`running/success/error/cancelled`）给业务页面用——那是执行槽，只属于排障页。新增业务状态时，先改 `states.py`，再改写入方，并跑 `dev/tests/test_task_semantics.py`。

构建：`cd frontend && npm run build` → 产物进 `static/`。

---

## 第 7 步 · 测试与记录

```bash
# 单条
python -m pytest dev/tests/test_xxx.py -q

# 全量（CI 跑的就是这套）
python -m pytest dev/tests -q
cd frontend && npm run test
```

必须补的三类断言：

1. **幂等**：同一输入执行两次，第二次不产生新记录
2. **停止**：`request_stop()` 后任务停在 `stopped`，不继续写
3. **不确定**：写超时后状态是 `uncertain`，且**没有**自动重放

最后：`CHANGELOG.md` 在 `## 未发布` 下记一条。**不要顺手改版本号**——版本由 Git tag 决定，见 `AGENTS.md`。

---

## 常见落点错误

| 症状 | 真实原因 | 正确落点 |
|---|---|---|
| 规则改了要跑整套容器才敢验证 | 规则写在 IO 层 | `app/domain/` |
| 加了功能但前端看不到进度 | 没走 `jobs`，只在内存跑 | `jobs.enqueue` + `jobs.progress` |
| 同一个东西被处理两次 | 没有唯一约束，靠先查后插 | `UNIQUE(...)` + `INSERT OR IGNORE` |
| 停止按钮点了没反应 | 循环里缺 `checkpoint()` | 每个耗时点前加 |
| 重试后出现重复文件 | `uncertain` 被当成可重试 | 停下等人看 |
| `engine` 越来越胖 | 新代码图省事用旧导出 | 直接 import 所属模块 |
| 改了前端不生效 | 改的是 `static/` 而不是 `frontend/src/` | 改源码后 `npm run build` |
| 测试跑不过但不是自己改的 | 动了业务逻辑又动了版本元数据 | 两件事分开提交 |

---

## 一句话

**规则进 `domain`，流程进 `cloud`，入口进 `routers`，状态进 `jobs`，界面进 `views`。**

五句话能覆盖 90% 的新增改动。剩下 10% 是外部适配器，进 `app/cloud/provider115.py` 那类文件。
