# 已知问题与环境陷阱

本文件记录「不是代码 bug、但会浪费你半天」的东西，以及已知未修的缺陷。新发现请往上加。

## 环境陷阱

### `organizer.py` 是 mixed EOL，不要用 read_text/write_text 改

```
$ git ls-files --eol backend/strimkeep/organizer.py
i/mixed w/mixed
```

用 Python 的 `Path.read_text()` / `write_text()` 读写会把整个文件的行尾统一，`git diff` 直接变成 **2700+ 行**的伪改动，无法 review，也没法比对真实改动。

正确做法：按字节读、保留行尾，按「整行内容」匹配后替换。

```python
raw = target.read_bytes().decode('utf-8')
lines = raw.splitlines(keepends=True)          # 每行保留自己的 \r\n 或 \n
# ... 按行匹配替换，新行沿用被替换区域第一行的行尾 ...
target.write_bytes(''.join(lines).encode('utf-8'))
```

改完确认 `git diff --stat` 只有预期行数。

### 经 ssh 传中文路径会乱码

PowerShell → ssh 传 `测试` 这类中文会变成 UTF-8 被按 GBK 解读的乱码（`娴嬭瘯`）。绕开办法：用 glob，让远端 shell 自己展开。

```sh
/vol1/1000/*/StrimKeep          # 线上目录
/vol1/1000/*/StrimKeep-dev      # 开发仓库
```

注意 **sh 不会在重定向目标里展开 glob**：`> /vol1/1000/*/StrimKeep/data/q.py` 会失败，必须先 `d=$(ls -d ...)` 再用 `"$d/..."`。

### heredoc 会被加上 CRLF

`@'...'@ | ssh nas "sh -"` 传输的脚本每行尾部会带 `\r`，表现为 `can't open '/tmp/x.txt\r'`、`syntax error near unexpected token`。用 base64 传文件再 `tr -d '\r'`：

```sh
printf '%s' '<base64>' | base64 -d | tr -d '\r' > /tmp/patch.py
```

注意 `tr -d '\r'` 里的 `\r` 不要写成 `\\r`，否则会连字母 `r` 一起删掉（`StrimKeep` → `StimKeep`）。

### 文件权限

NAS 上多数文件权限显示为 `----------`，`chmod` 往往只影响 exec 位。git 只比较 exec 位：脚本类文件应为 `100755`，普通文件 `100644`。用 `git ls-files -s` 核对，用 `git update-index --chmod=+x <路径>` 修（改完记得 `chmod +x` 让工作区一致，否则 `git status` 会一直显示 `M`）。

## 测试相关

### 门禁并发会 OOM（退出码 137）

`scripts/verify.sh` 分 6 组跑，注释里就写了「两个测试容器并行会 OOM」。NAS 同时运行着线上容器时更容易触发。看到 `137` 不要当成断言失败：

```sh
# 单独重跑那一组
sh scripts/test_in_container.sh strimkeep-dev:0.8.3 tests/test_xxx.py tests/test_yyy.py
```

### `verify.sh` 的清单必须和 tests 目录一致

0.8.2 时清单里有 `tests/test_deep_delete_task.py`，但文件根本不存在，导致门禁第一组直接 `ERROR: file or directory not found` 退出码 4，整个门禁跑不起来。0.8.3 已移除该行并把 `test_multilang_identify.py` 加入「识别与命名」组。

新增或删除测试文件后，请同步 `scripts/verify.sh`。

### 已修复（0.8.4）：TMDB 500 时条目停在 `discovered`

```
tests/test_identity_real_names.py::test_tmdb_failure_is_reported_not_guessed
E   assert 'discovered' in ('pending', 'review')
```

TMDB 返回 HTTP 500 时，整理条目没有落到 `pending`/`review`，停在 `discovered`，界面上看起来像「文件没被处理」。在**未改动的 0.8.2 镜像**上同样复现，不属于版本回归；该用例也不在 `scripts/verify.sh` 门禁清单内。

原因与修法：`organizer.halt()` 现在把 `discovered` 状态的条目一并归到 `review` 并写入失败原因，0.8.4 起 `test_identity_real_names.py` 全绿。0.8.3 及以前存在此问题。

### 已修复（0.8.8）：任务中心三个分类问题

| 现象 | 原因 | 修法 |
| --- | --- | --- |
| 「深度删除」把「进行中」挤到旁边，看着像挡住分类 | 它是 `DeepDeleteHistory` 组件，却被当成一个分类 `el-tab-pane` 插在「全部」和「进行中」之间 | 移到最后一个 tab（「失败」之后）；它仍是独立视图，不参与任务分类 |
| 「未识别」永远是 0，从没显示过内容 | `bucketOf()` 用 `counts.pending > 0` 判定，而 `pending` 只是中间态，跑完就变 `done`/`skipped` | 后端 `job_public` 新增 `unidentified`（按 `organize_items.step LIKE '%未识别%'` 统计）；前端改用它，`pending` 仅作兜底 |
| 「过滤」永远是 0 | 条件是 `!job.total && job.scan_filter.scanned`，要求一个文件都没进队列 | 改为 `!counts.done && scan_filter.excluded > 0`：有文件被扩展名/关键词/体积挡掉，且这批没有成功入库 |

## 已知未修

- **STRM 失效清理（0.8.10 / 0.8.12 已修）**：账号级 revision 变化时继续逐条核对云端存续；0.8.12 补齐扫描开始时间保护，时间证据缺失时跳过清理。
- **裸集号文件不做父目录兜底（2026-11-03 决定不做）**：`media.py` 对 `27.mkv`、`S01E01-1080P.mkv` 这类直接 `NeedsReview('文件名只有集号，缺少作品线索')`。`parse_name()` 其实已经实现了父目录兜底，门槛也硬（目录必须带年份/季号/tmdb id 之一，文件名与目录 TMDB 冲突或季号冲突一律停手），但分享通道 4 个 `parse_name` 调用点都只传文件名，且 `share_strms` 表没存原始目录，所以对分享来源完全不生效。要做需要给 `share_strms` 加 `folder` 列、生成时写入、三处传参，属主流程改造。**结论：不做，裸集号留在「未识别」由人工处理**（现场累计 24 条事件）。
- **`playbacks` 里的无引用记录**：STRM 写的是 `/play115/{pickcode}/...`，取链不查库，这张表只剩历史记账（现场 747 条 / 65 条已撤销）。0.8.8 提供「清理未被引用的播放身份」，默认只删已撤销且无引用的记录；未撤销的孤儿需要 `include_active=1` 才会动。
