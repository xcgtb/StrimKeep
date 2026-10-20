# StrimKeep 上游与自建双来源源码包

- 基线发布：v1.6.4；提交 daec694eac23941359414ce883a878fcc459947b。
- 本次按 1.6.5 发布；镜像构建成功后再更新 NAS 容器。
- 完整源码包含四来源统计、全局 Emby 四路径映射、来源展示、分来源删除核验、回归测试与构建后的 Vue 页面。不含 .git、运行数据、授权、node_modules 或私人部署配置。已经包含编译资源，NAS 覆盖和推送时不需要安装 Node.js。
- 保留上游与自建两套挂载。更新后按 docs/DUAL_MEDIA_SOURCES.md 配置自建 Emby 路径，确认挂载后刷新总览及同步 Emby；不会替你猜测实际 Emby 目录或自动开启治理删除权限。
- 验收见 dev/acceptance-dual-media-sources.md。

解压 StrimKeep/ 后复制源码到已有 Git 工作区，保留原 .git、实际部署配置与运行数据。旧构建资源仅按 dev/obsolete-assets-after-v1.6.4.txt 的明确清单删除。后续发布须使用新的不可变标签，不覆盖 v1.6.4。包内历史发布脚本不适用于此次修改；仅覆盖源码不会更新容器。
