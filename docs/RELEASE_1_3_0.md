# v1.3.0 飞牛发布步骤

本包是基于 1.2.12 的 115 工作台源码，已将更新说明移入 `1.3.0`。Git Tag 必须为 `v1.3.0`，镜像标签为 `1.3.0` 和 `latest`，不要使用只有两段的 `v1.3`。不修改 `app/version.py`，发布工作流会注入运行版本。

## 放置源码

把 `StrimKeep-v1.3.0-source.zip` 上传到 `/vol1/1000/git/`，解压后覆盖 `/vol1/1000/git/StrimKeep/` 的源码。压缩包有一层 `StrimKeep/` 目录，所以解压目标为 `/vol1/1000/git/`。已有 Git 工作区的 `.git` 与部署目录 `/vol1/1000/docker/StrimKeep` 不在压缩包中。

前一份无版本的 115 源码若已覆盖，可直接用本包更新源码；不要复用旧 `v1.2.12` 标签。若已自行修改代码或提交，先检查差异，不能直接覆盖私人改动。

本地 `main` 应先与远程同步，再覆盖源码；源码覆盖后远程若有新提交，不要强制 reset 或强推。提交前只运行下面无第三方依赖的发布边界检查，不需要在 NAS 运行整套开发测试。

## 提交、打标签、推送

在仓库根目录执行：

```bash
git switch main
git status --short
GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.3.0 python3 dev/tools/release_check.py
```

检查成功、待提交文件确为本次源码后，暂存：

```bash
git add -u
git add app/cloud app/routers/cloud.py requirements-cloud115.lock \
  static/js/cloud-workbench.js static/css/cloud-workbench.css \
  dev/tests/test_cloud_workbench.py dev/tests/verify_cloud_workbench_ui.cjs \
  dev/CLOUD115_VERIFICATION.md docs/CLOUD115_WORKBENCH.md \
  docs/CLOUD115_DEPENDENCIES.md docs/RELEASE_1_3_0.md
git diff --cached --stat
```

确认差异后，下列命令依赖成功才继续，避免提交失败后标签落到旧代码上：

```bash
git diff --cached --check && \
git commit -m "release: v1.3.0 Python 115 workbench" && \
git tag -a v1.3.0 -m "StrimKeep v1.3.0" && \
git push --atomic origin main v1.3.0
```

若提示已有同名标签或远程分支更新，停止处理具体原因，不用 `-f` 移动标签或覆盖远程。推送时 GitHub Actions 的标题应为「StrimKeep v1.3.0 · 发布镜像」，会先执行 Python/前端回归及生产镜像检查，再发布 amd64/arm64。

## 镜像完成后

等 Actions 全部成功后，在实际部署目录执行：

```bash
cd /vol1/1000/docker/StrimKeep
docker compose pull strimkeep
docker compose up -d strimkeep
```

管理界面仍使用原端口、原账号密码与数据挂载；启用网盘工作台内的 Emby 反代后，播放器才改连所配置的反代端口。host 网络沿用现状，桥接网络需映射额外端口。

此源码的自动验证通过；真实账号、播放器联调仍待验收。发布标签并不表示这些已验证。OpenAPI 授权、官方删除、其他网盘不包含在 1.3.0。闭源分发的许可冲突仍未解决，不能因本次改版本而认为已取得闭源授权。
