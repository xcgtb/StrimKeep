# 文档导航

> 只放**手册**和**状态**。过程记录、历史版本说明都在归档里，别在这里堆。

## 先看这一个

- **[STATUS.md](STATUS.md)** — 唯一权威现状：在跑什么版本、这版改了什么、什么验证过、下一步做什么。
  **与任何其它文档冲突时，以它为准。**

## 运维手册（operations/）

| 文件 | 内容 |
| --- | --- |
| [RATE_LIMITS.md](operations/RATE_LIMITS.md) | 115 请求节奏、风控预案、405 的独立处置 |
| [METRICS.md](operations/METRICS.md) | `/metrics` 指标口径与开启方式、建议告警、慢请求排查顺序（0.8.22） |
| [LIBRARY_PATHS.md](operations/LIBRARY_PATHS.md) | 本地 STRM 路径怎么来的、三方对照、库根改名与回滚、Emby 库根要改成什么（0.8.30） |
| [CHANNEL_MONITOR.md](operations/CHANNEL_MONITOR.md) | 频道监控的设计与边界 |
| [SYNC_ISOLATION_20261029.md](operations/SYNC_ISOLATION_20261029.md) | 同步独立子进程的隔离设计 |
| [MODULE_REMOVAL.md](operations/MODULE_REMOVAL.md) | 0.7.0 模块精简范围 |


实测记录，未反编译。作者在镜像内附了禁止分析声明，这里只记录外部可观测行为。

| 文件 | 内容 |
| --- | --- |
| [PIPELINE-SPEC.md](upstream/PIPELINE-SPEC.md) | **主线**：115 调用序列、落盘产物、四段日志、性能与限流实测 |
| [_ALIGNMENT.md](upstream/_ALIGNMENT.md) | 识别/命名口径的 26 条同题对照（10-28 快照） |
| [_AUTOMATION.md](upstream/_AUTOMATION.md) |  14 个自动化循环（10-29 快照） |
| [_BEHAVIOR.md](upstream/_BEHAVIOR.md) | 单条资源的处理行为 |
| [_PIPELINE.md](upstream/_PIPELINE.md) | 整理管线的分段 |
| [_SCHEDULING.md](upstream/_SCHEDULING.md) | 定时与调度口径 |
| [_SYNC_SPEED.md](upstream/_SYNC_SPEED.md) | 全量同步为什么能秒级 |

## 归档（archive/）

只作历史追溯，**内容可能与当前版本不一致**，不要照着做。

| 文件 | 说明 |
| --- | --- |
| WORK_LOG.md | 过程日志，到 10-29 02:28 |
| ACCEPTANCE.md | 0.7.0 验收评审 |
| RELEASE-0.7.0.md | 0.7.0 发布说明 |
| UI_HOTFIX_README.txt / UI_PATCH_README.md | 0.6.0 的 UI 覆盖包说明（已无用） |
| StrimKeep_Project_Plan_v1.1.docx | 最初的项目计划书 |

## 发布历史（releases/）

0.2.0 → 0.6.5 的逐版说明与审计记录，原样保留。

## 法律

[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt) — 第三方许可证。
