# GitHub 发布

仓库根目录保留应用、前端、公共 YML、Dockerfile、README 和 `.github/`。测试及维护工具集中在 `dev/`，CI 使用它们，生产镜像排除它们。

将源码包解压后的内容放到 Git 工作区，让 `app/`、`static/`、`README.md` 直接位于仓库根目录。不要把压缩包本身当作仓库源码，也不要提交运行数据、实际密码或密钥。

## 提交前

使用开发依赖执行：

```bash
python dev/tools/release_check.py
python dev/tools/static_check.py
pytest dev/tests/ -q
npm ci --prefix frontend
npm --prefix frontend run format:check
npm --prefix frontend test
npm --prefix frontend run build
node dev/tools/verify_frontend.cjs
npm --prefix frontend run test:e2e
npm --prefix frontend run test:production
git status --short
```

根目录的 `docker-compose.yml` 是可提交的公共模板，凭据必须保持占位值。私人配置放 Git 工作区外的部署目录，或使用已忽略的 `docker-compose.override.yml`；不要修改公共模板为自己的密码再提交。

## 自动发布（版本 Tag 触发）

只有 `vX.Y.Z` Tag 推送会自动触发一次 `publish-image`，Github Actions 运行标题和蓝色引用标签均显示 `vX.Y.Z`（例如 `v1.2.9`），方便确认目前发布到哪一版。`main` 推送本身不发布镜像，也不触发第二套检查；Pull Request 仍独立运行 `checks.yml`。

正式发布必须先把 `CHANGELOG.md` 中的版本标题 `## X.Y.Z（日期）` 写好并提交。发布前检查远程 Tag，确保未复用旧版本号：

```bash
git fetch --tags origin
git tag --list 'v*' --sort=-v:refname | head -n 10
git tag --list v1.2.9
git status --short
GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.2.9 python3 dev/tools/release_check.py
# 检查无误后提交本次实际变更；不使用 git add .，避免混入私人文件
git add <本次实际修改的文件>
git diff --cached --check
git commit -m "release: v1.2.9 - share batch and mobile selection"
git tag -a v1.2.9 -m "StrimKeep v1.2.9"
git push --atomic origin main v1.2.9
```

发布流程严格核对 Tag 指向当前提交，并检查对应 CHANGELOG。通过 Python、前端和生产镜像回归后，同一次 amd64/arm64 镜像构建仅发布 `latest` 与 `X.Y.Z`，不发布 `X.Y` 这种浮动次版本标签。Docker 镜像标签 `X.Y.Z` 与源码 Git Tag `vX.Y.Z` 是刻意不同的命名规范。

只推送 `main` 不会自动构建镜像；即使晚些时候单独推送新 Tag，也会触发该版本的发布。`workflow_dispatch` 保留手工运行权限，手工运行时请选择目标版本 Tag；选择 `main` 会显示 `main`，仅用于特殊场景，避免当作常规正式发布。

版本由 Git 标签注入构建参数 `APP_VERSION`；不要修改 `app/version.py` 的本地兜底值 `0.0.0+local`。
`latest` 仍适合 NAS 测试，待镜像构建成功后在部署目录执行：

```bash
docker compose pull strimkeep
docker compose up -d strimkeep
```

Tag 是已公开的不可变发布记录。测试或镜像构建失败时保留失败标签，修复后创建下一个版本，不强制移动旧 Tag。
GitHub Release 不是必须的，正式发布后可单独创建发行说明。

覆盖源码包时，`dev/docker-compose.build.yml` 是需要一起更新的开发模板。只保护实际部署目录里的配置、密码和数据；不要排除开发模板。推送前运行 `python3 dev/tools/release_check.py`，避免将版本不一致的文件推送到 CI。
