# StrimKeep 双库扫描与海报性能优化（2026-10-10）

## 边界与变更

本补丁以用户提供的 `StrimKeep-perf-source.tar.gz` 为源码基线，**不是新发布版本、不打 tag**。

- **双库扫描**（`app/lib.py`）：普通 STRM 从 `stat()` 与 `is_symlink()` 两次元数据调用改为一次 `lstat()`，拒绝软链接及非普通文件，保留挂载故障时整次扫描失败并保留旧索引。每个剧集目录的标题/年份/TMDB 身份一次计算，文件级季集解析和现有 SQLite 增量索引保持不变；没有改路径匹配或删除准则。
- **TMDB 图片代理**（`app/posters.py`）：同一海报首次请求的多个调用者合并为一次 CDN 拉取；其他请求等同一份结果或错误，故障可重试。保留 CDN 白名单、大小限制、Content-Type、TTL 与容量上限。
- **浏览器海报**（`static/js/runtime.js`）：影视探索与片库映射公用的 `posterFetch()` 加入 20 MiB / 64 条上限的会话内存 LRU，重用 CacheStorage open Promise，并把持久缓存写入改为不阻塞显示的 best-effort 操作。保留 6 路图片并发、首屏优先/懒加载、远端代理、鉴权、退化占位、后台预热。
- **不改变**：双库文件发现范围、季集识别、TMDB/Emby 配对、七维质量排序、治理规则、删除预览/执行、缺集角标、订阅、数据结构、容器 Compose、版本号及页面布局。

## 自动化验收结果

| 项目 | 结果 | 限制 |
|---|---|---|
| 修改前 Python 回归 | 786 项通过 | Python 3.13.5（本环境） |
| 修改后 Python 回归 | **789 项通过** | 增加 3 项扫描/海报测试 |
| 修改前前端回归 | 26 组通过 | Node 22.16、DOM 替身 |
| 修改后前端回归 | **27 组通过** | 增加内存/持久化/并发测试 |
| Python 静态检查与发布边界 | 通过 | `dev/tools/` |
| 历史 SQLite schema 兼容性 | **4/4 通过** | 临时数据库 |
| 真实本地 HTTP/SQLite 重启验收 | **2/2 通过** | 本地 HTTP、临时数据及媒体目录；未接 NAS |
| Docker 镜像构建 | 未执行 | 当前工具环境未安装 Docker CLI/daemon |
| 飞牛/CD2/Emby/115 真实环境 | 未执行 | 需要目标 NAS 联调 |

### 本机扫描基准（仅供参考）

测试脚本生成 30 个剧集目录、共 2,400 个 `.strm`，通过内存索引桩排除磁盘数据库读写干扰，分别执行 3 次冷索引及 3 次复扫，取中位数：

| 场景 | 修改前 | 修改后 | 变化 |
|---|---:|---:|---:|
| 冷索引扫描 | 31.3 ms | 27.6 ms | 约 11.8% 更短 |
| 已缓存复扫 | 27.1 ms | 24.5 ms | 约 9.6% 更短 |

该测试仅验证本机文件系统与数据形状，不代表飞牛挂载目录的延迟；大量远程目录时收益取决于 CD2/文件系统元数据延迟。**未获得真实 NAS 扫描耗时和首次海报呈现耗时，不宣称实际百分比。**

## 飞牛部署建议与安全验证

1. 在 Git 工作区备份/确认 `git status --short` 为空后，应用优化包；如有本地更改先保存，不覆盖未提交工作。
2. 可运行 `python3 dev/tools/static_check.py`、`python3 dev/tools/release_check.py`、`node dev/tools/verify_frontend.cjs`、`python3 -m pytest dev/tests/ -q`。上述均只使用开发测试环境。
3. 在与生产挂载、配置隔离的测试容器中验证双库只读扫描结果，对比治理计划、STRM 数量、季集数、低置信度不删除，以及离线挂载不发布结果。
4. 对比影视探索/片库映射的首屏与返回页面速度、海报缺失回退、Emby 鉴权、移动端网络/断网、缓存清理。确认后再更新生产容器。
5. 本优化源码 **不应直接写入生产 `data/`**。如需回滚，恢复 Git 工作区原提交并重新构建/拉取以前的镜像；不要重用旧 `latest` 当作固定回滚点。

## 本地验收命令

```sh
python dev/tools/static_check.py
python dev/tools/release_check.py
node dev/tools/verify_frontend.cjs
python -m pytest dev/tests/ -q
python dev/checks/verify_docs_schema_compat.py
python dev/checks/verify_release_startup.py
```
