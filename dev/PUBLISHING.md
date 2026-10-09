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

## 发布镜像

首次版本为 1.0.0。源码推送完成后创建并推送对应标签：

```bash
git tag v1.0.0
git push origin v1.0.0
```

`publish-image` 先执行 Python、前端和生产镜像检查，再发布 amd64/arm64 镜像。标签须与 `app/version.py` 一致；失败时先修复检查，不绕过 CI。

预设地址为 `ghcr.io/xcgtb/strimkeep:1.0.0`。实际发布流程按 GitHub 仓库名生成小写镜像地址。如果更换账号或仓库名，同步修改公共 YML、开发 YML、README 和 Dockerfile 的 source 标签。发布后将 GHCR 包设为公开，普通用户才能不登录直接拉取。

以后升级同步修改 `app/version.py`、Dockerfile 的 `APP_VERSION`、两份 YML 的镜像版本、README 的部署示例和 CHANGELOG，再运行发布检查。已推送的版本标签不覆盖。
