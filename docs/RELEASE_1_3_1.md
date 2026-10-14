# v1.3.1 镜像安装阶段修复

1.3.0 的应用回归通过，但 Actions 在「Build runtime-only image」失败，后续镜像发布被跳过。源码中的 Dockerfile 只复制 `requirements*.txt`，无法匹配 `requirements-cloud115.lock`；在相同复制规则下可复现 pip 报错找不到锁文件。

本次明确复制 `requirements.txt` 与 `requirements-cloud115.lock`，并在发布边界检查中增加依赖引用文件检查，防止同类漏包。没有改变 115 业务与治理逻辑。保留 `v1.3.0`，修复使用新标签 `v1.3.1`。

补丁包仅包含本次修改的文件，适用于已经提交的 1.3.0 工作区，解压覆盖源码，不动部署目录和数据。

```bash
unzip -o /vol1/1000/git/StrimKeep-v1.3.1-image-fix.zip -d /vol1/1000/git/
cd /vol1/1000/git/StrimKeep
GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.3.1 python3 dev/tools/release_check.py
git add Dockerfile CHANGELOG.md MANIFEST.sha256 dev/tools/release_check.py \
  dev/tests/test_release_check.py docs/RELEASE_1_3_1.md
git diff --cached --check && \
git commit -m "release: v1.3.1 fix missing Docker dependency lock" && \
git tag -a v1.3.1 -m "StrimKeep v1.3.1" && \
git push --atomic origin main v1.3.1
```

镜像发布成功后再拉取 `latest`。本次全量 Python 回归 1,178 项通过；在本地复制 Docker 安装阶段文件后，pip 离线 dry-run 已通过，漏复制锁文件的回归已覆盖；该环境没有 Docker，不能把这些检查称为真实容器构建通过。实际 Docker 与多架构镜像检查仍由 Actions 执行。
