# 开发与测试

此目录集中维护者工具。普通部署使用仓库根目录的 `docker-compose.yml`；生产镜像不包含 `dev/`，只内置运行健康检查。

| 目录 | 内容 |
| --- | --- |
| `tests/` | Python 回归、前端交互回归、固定输入与预期结果 |
| `checks/` | 历史 SQLite、HTTP 启动、单写存储与探索后台的隔离检查 |
| `tools/` | 静态检查、发布边界、前端检查、只读存储审计 |
| `requirements.txt` | 开发依赖，包含生产依赖与 pytest/httpx |
| `docker-compose.build.yml` | 仅供开发者本地构建镜像 |

## 本地回归

从项目根目录执行，使用 Python 3.11 和 Node 20：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r dev/requirements.txt
python dev/tools/static_check.py
python dev/tools/release_check.py
pytest dev/tests/ -q
node dev/tools/verify_frontend.cjs
```

前端用例使用 DOM 替身，不能代替真实手机/电脑视觉验收。隔离测试使用临时状态和假服务，不操作真实媒体，也不发送通知。

## 开发构建

修改 `dev/docker-compose.build.yml` 的密码和媒体路径，在项目根目录执行：

```bash
docker compose -f dev/docker-compose.build.yml up -d --build
```

构建上下文为项目根目录，数据挂载到根目录的 `data/`。开发模板与日常镜像模板同名容器，不能同时启动两套。

## 仅生产依赖的验收

构建生产镜像后，从项目根目录将脚本通过 stdin 送入容器，不需要把开发工具打进镜像：

```bash
docker build -t strimkeep:check .
docker run --rm --network none -i --entrypoint python strimkeep:check - < dev/checks/verify_docs_schema_compat.py
docker run --rm --network none -i --entrypoint python strimkeep:check - < dev/checks/verify_release_startup.py
docker run --rm --network none -i --entrypoint python strimkeep:check - < dev/checks/verify_sqlite_single_store.py
docker run --rm --network none -i --entrypoint python strimkeep:check - < dev/checks/verify_explore_background.py
```

真实 NAS 的只读空间诊断：在源码根目录执行 `docker exec -i strimkeep python - < dev/tools/storage_audit.py`。日常运行状态在 Web 和日志中心查看。

[发布步骤](PUBLISHING.md) · [验收记录](VERIFICATION.md) · [架构](../docs/ARCHITECTURE.md)
