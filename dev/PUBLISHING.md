# GitHub 发布

仓库根目录保留应用、前端、公共 YML、Dockerfile、README 和 `.github/`。测试及维护工具集中在 `dev/`，CI 使用它们，生产镜像排除它们。

将源码包解压后的内容放到 Git 工作区，让 `app/`、`static/`、`README.md` 直接位于仓库根目录。不要把压缩包本身当作仓库源码，也不要提交运行数据、实际密码或密钥。

## 提交前

使用开发依赖执行：

```bash
python dev/tools/release_check.py
python dev/tools/static_check.py
pytest dev/tests/ -q
node dev/tools/verify_frontend.cjs
git status --short
```

根目录的 `docker-compose.yml` 是可提交的公共模板，凭据必须保持占位值。私人配置放 Git 工作区外的部署目录，或使用已忽略的 `docker-compose.override.yml`；不要修改公共模板为自己的密码再提交。

## 日常更新 latest

推送 `main` 后，`publish-image` 自动运行回归检查，再构建并发布 amd64/arm64 的 `ghcr.io/xcgtb/strimkeep:latest`。等待该工作流的 `build` 成功后，NAS 在原 Docker 部署目录执行：

```bash
docker compose pull strimkeep
docker compose up -d strimkeep
```

部署配置需使用 `ghcr.io/xcgtb/strimkeep:latest`。仅重启容器不会更新镜像。可以在 Actions 手动运行 `publish-image` 并选择 `main`，重建 latest。

日常 main 构建只更新 latest，不覆盖正式版本号镜像。应用使用从 Git 标签自动解析并注入的正式版本，例如 `1.0.3`。Web 仅显示版本号，不显示提交编号；`latest` 的代码可能领先于正式版本标签。

## 正式版本发布

版本由 Git 标签自动注入镜像，无需手动修改源码版本号。源码推送完成后创建并推送下一版本标签，例如：

```bash
git tag v1.0.4
git push origin v1.0.4
```

`publish-image` 先执行 Python、前端和生产镜像检查，再发布 amd64/arm64 镜像。标签必须使用 `v数字.数字.数字` 格式；`v1.0.4` 自动生成 `1.0.4` 镜像并让 Web 显示 `1.0.4`。失败时先修复检查，不绕过 CI。

当前已发布固定版本地址为 `ghcr.io/xcgtb/strimkeep:1.0.3`，日常部署模板继续使用 `latest`。实际发布流程按 GitHub 仓库名生成小写镜像地址。如果更换账号或仓库名，同步修改公共 YML、开发 YML、README 和 Dockerfile 的 source 标签。发布后将 GHCR 包设为公开，普通用户才能不登录直接拉取。

以后正式升级只需选择新 Git 标签，CI 自动确定版本，无需修改 Dockerfile、Compose 或 `app/version.py`。`app/version.py` 只存放 `0.0.0+local` 本地运行兜底标识，**不得**据此判断正式版本。无有效 Git 标签的 CI 镜像构建必须失败。CHANGELOG 顶部保留「未发布」，发版前将本次内容移入新版本标题并同步文档示例。开发 Compose 固定使用 `strimkeep:local`，公共部署模板继续使用 `latest`。已发布成功的版本标签不覆盖。

覆盖源码包时，`dev/docker-compose.build.yml` 是需要一起更新的开发模板。只保护实际部署目录里的配置、密码和数据；不要排除开发模板。推送前运行 `python3 dev/tools/release_check.py`，避免将版本不一致的文件推送到 CI。
