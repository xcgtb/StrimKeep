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

## 自动发布（main + Tag 一次推送）

`main` 推送仅触发一次 `publish-image`：先复用 `checks.yml` 的 Python、前端和生产镜像检查，
全部通过后 **只构建一次** amd64/arm64 镜像。`checks.yml` 仍独立用于 Pull Request，
**Tag 推送本身不再触发第二套检查和构建**。

正常修复必须先在 `CHANGELOG.md` 添加对应版本的 `## X.Y.Z（日期）` 标题，再执行：

```bash
git add <本次实际修改的文件>
git commit -m "fix: describe changes"
git tag -a v1.2.8 -m "StrimKeep v1.2.8"
git push --atomic origin main v1.2.8
```

`--atomic` 要求 GitHub 同时接收 `main` 和 Tag，防止发布工作流在 Tag 尚不可见时启动。
`publish-image` 检查 **是否有且只有一个符合 vX.Y.Z 的版本标签指向当前 HEAD**：

- 如果有，先核实 `CHANGELOG` 与标签一致，然后**同一次构建**推送 `latest` 和 `X.Y.Z`。
- 如果没有，只更新 `latest`，版本号沿用当前分支最近的已发布版本，**不会覆盖固定版本标签**。
- 如果一个提交有多个有效版本标签，拒绝发布，避免镜像版本不确定。
- 单独补推 Tag 不触发发布；如已先推 `main` 后补 Tag，需在 Actions 手动运行 `publish-image`（选择 `main`）重新测试并发布。

版本由 Git 标签注入构建参数 `APP_VERSION`；不要修改 `app/version.py` 的本地兜底值 `0.0.0+local`。
`latest` 仍适合 NAS 测试，待镜像构建成功后在部署目录执行：

```bash
docker compose pull strimkeep
docker compose up -d strimkeep
```

Tag 是已公开的不可变发布记录。测试或镜像构建失败时保留失败标签，修复后创建下一个版本，不强制移动旧 Tag。
GitHub Release 不是必须的，正式发布后可单独创建发行说明。

覆盖源码包时，`dev/docker-compose.build.yml` 是需要一起更新的开发模板。只保护实际部署目录里的配置、密码和数据；不要排除开发模板。推送前运行 `python3 dev/tools/release_check.py`，避免将版本不一致的文件推送到 CI。
