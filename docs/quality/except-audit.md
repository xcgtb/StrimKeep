# 静默吞错审计清单

由 `tools/except_audit.py` 生成（AST 精确匹配：处理体只有 `pass` / `return` / `continue` / `break`）。

## 怎么判定

**进度：全兜底 27 处，已判 27 处，待判 0 处。**

逐条问三个问题，全「否」就需要补留痕：

1. **调用方会用别的方式记录这次失败吗？**（例如上游 `except` 会写 `store.event`、条目状态会置 `needs_review`）
2. **这个失败是预期内的常态吗？**（`FileNotFoundError`、解析可选字段的 `ValueError`、关闭句柄的 `OSError`）
3. **静默的后果可控吗？**（最坏情况是少一个可选字段，还是「功能不工作且查不到原因」）

判定结果写在每条的「结论」列：`合理` / `需留痕` / `已补`。

## 一、全兜底静默（27 处，优先级最高）

`except Exception` 一把抓、且不留任何痕迹——用户看到的只是「没反应」。

| 文件 | 行 | 处理体 | 结论 |
| --- | --- | --- | --- |
| archive.py | 334 | pass | **合理**（注释写明：变更条目走既有查找流程，这条预检失败不影响归档） |
| cover_service.py | 41 | pass | **合理**（停机收拢后台任务，异常与 CancelledError 一并忽略是预期） |
| emby.py | 68 | return | **合理**（连接预检只是排障手段，失败不影响真正的通知与交付） |
| explore_api.py | 70 | continue | **合理**（Emby 查询失败即不把该类型标记为完整，调用方据此不下"不在库"结论） |
| explore_api.py | 143 | continue | **合理**（单部剧的详情拉取失败只让集数统计偏小，属展示层） |
| explore_api.py | 234 | return | **合理**（拿不到集数就返回 0，属展示层） |
| metadata_files.py | 105 | return | **合理**（写入失败有返回值 False，调用方据此决定是否复算） |
| organize_options.py | 126 | pass | **合理**（TMDB 中文标题解析失败就回退用原名） |
| organizer.py | 243 | return | **合理**（probe_login 是低频探测（10 分钟一次），失败只是下次再试，无状态变更） |
| organizer.py | 1216 | pass | **合理**（TMDB 预取暖场失败，主流程会照常再查一次） |
| organizer.py | 1327 | pass | **合理**（预分析失败的条目仍是 discovered 且无 plan，主流程会重新准备这一条） |
| organizer.py | 1827 | pass | **合理**（洗版单条替换失败由条目状态/消息记录，外层 replace() 负责） |
| outputs.py | 51 | return | **合理**（token 解密失败返回空归属集合，后果是保守（不清理）） |
| share_api.py | 135 | pass | **合理**（撤销已写库；本地 strm 清理失败只是残留，下次巡检会处理） |
| share_api.py | 194 | continue | **合理**（重出现的同 file_id 跳过，属监控语义） |
| share_service.py | 344 | pass | **合理**（TMDB 预取暖场失败，主流程会照常再查一次） |
| share_service.py | 656 | pass | **合理**（注释写明：缓存写入不能把成功输出变成失败） |
| share_service.py | 806 | pass | **合理**（撤销已写库；本地 strm 清理失败只是残留） |
| sync.py | 705 | pass | **待观察**（删目录后的附属文件清理是尽力而为；失败只留残留，不影响台账） |
| sync.py | 749 | return | **待观察**（files JSON 解析失败就跳过该行修复；后续巡检仍会报待修复） |
| sync.py | 824 | return | **合理**（JSON 解析失败被显式判为 broken，巡检会把它报出来） |
| sync.py | 835 | return | **合理**（显式归类为 broken，巡检会报出来） |
| sync.py | 862 | return | **合理**（显式归类为 broken，巡检会报出来） |
| sync.py | 882 | return | **合理**（解析失败即不认为 unchanged，判断偏保守） |
| sync_worker.py | 44 | pass | **合理**（finally 里关 HTTP 客户端，失败无副作用） |
| telegram_bot.py | 158 | pass | **合理**（仅用于取任务标签做展示，失败回退到目标目录名） |
| telegram_bot.py | 205 | pass | **合理**（已经先回过"没能处理完"，再发通知失败可忽略） |

## 二、窄异常静默（56 处，多为合理）

只吞特定异常（文件不存在、字段解析失败、目录遍历权限等），通常属于「预期内」。

| 文件 | 行 | 异常 | 处理体 |
| --- | --- | --- | --- |
| archive.py | 231 | (KeyError, ValueError, IndexError, TypeError) | pass |
| channel_monitor.py | 376 | ValueError | return |
| channel_monitor.py | 504 | (ValueError, KeyError) | pass |
| cleanup.py | 137 | FileNotFoundError | pass |
| cleanup.py | 139 | OSError | continue |
| console_api.py | 29 | ValueError | return |
| console_api.py | 114 | (httpx.HTTPError, ValueError) | return |
| explore_api.py | 63 | (ValueError, TypeError) | continue |
| fsops.py | 26 | FileNotFoundError | return |
| fsops.py | 28 | OSError | return |
| fsops.py | 41 | OSError | pass |
| fsops.py | 43 | OSError | return |
| fsops.py | 55 | OSError | break |
| fsutil.py | 41 | OSError | return |
| fsutil.py | 66 | FileExistsError | return |
| fsutil.py | 68 | OSError | pass |
| fsutil.py | 72 | FileExistsError | return |
| main.py | 796 | NeedsReview | return |
| network.py | 16 | OSError | pass |
| network.py | 17 | (ImportError, OSError) | pass |
| notifications.py | 57 | OSError | continue |
| notifications.py | 215 | (httpx.HTTPError, ValueError) | return |
| offline_api.py | 215 | (TypeError, ValueError) | pass |
| organizer.py | 175 | asyncio.CancelledError | pass |
| organizer.py | 185 | asyncio.CancelledError | pass |
| organizer.py | 273 | (TypeError, ValueError) | continue |
| organizer.py | 804 | ValueError | pass |
| organizer.py | 1194 | (TypeError, ValueError) | pass |
| organizer.py | 1198 | NeedsReview | continue |
| organizer.py | 1252 | (TypeError, ValueError) | pass |
| organizer.py | 1325 | (NeedsReview, UncertainState) | pass |
| organizer.py | 1489 | UncertainState | continue |
| organizer.py | 2247 | NeedsReview | pass |
| organizer.py | 2419 | Stopped | pass |
| outputs.py | 62 | (ValueError, TypeError) | pass |
| outputs.py | 174 | OSError | break |
| quality.py | 156 | (ValueError, TypeError, OverflowError) | return |
| quality.py | 171 | (ValueError, ZeroDivisionError, OverflowError) | return |
| recycle.py | 18 | (ValueError, TypeError, OverflowError) | return |
| share_service.py | 57 | NeedsReview | return |
| share_service.py | 105 | (OSError, UnicodeError, ValueError) | return |
| share_service.py | 325 | NeedsReview | continue |
| share_service.py | 455 | (ValueError, TypeError, OSError) | pass |
| share_service.py | 697 | OSError | return |
| share_service.py | 774 | NeedsReview | continue |
| share_service.py | 875 | (OSError, UnicodeError, ValueError) | return |
| share_service.py | 883 | OSError | break |
| store.py | 224 | (ValueError, TypeError) | continue |
| store.py | 268 | (TypeError, ValueError) | return |
| sync.py | 712 | OSError | break |
| sync.py | 849 | OSError | return |
| sync.py | 1029 | OSError | pass |
| sync_hash_worker.py | 37 | (OSError, ValueError) | return |
| sync_hash_worker.py | 82 | ProcessLookupError | pass |
| untyped_cleanup.py | 14 | UnicodeDecodeError | return |
| upgrade.py | 100 | OSError | pass |

## 三、已核查过的高风险点

| 位置 | 判定 | 依据 |
| --- | --- | --- |
| `sync_worker.py:44` | 合理 | 在 `finally` 里关 HTTP 客户端，失败无副作用 |
| `organizer.py:1827` | 合理 | 洗版批次的单条替换失败由条目状态/消息记录，外层 `replace()` 负责 |
| `cover_service.py:41` | 合理 | 停机时收拢后台任务，`CancelledError` 与异常一并忽略是预期的 |
