# StrimKeep v1.6.2 盘内库存与整理预览源码包

- 基线：`v1.6.1`，Git SHA `9b9aaeb06104ae184d7c80090f8da37c9802706f`。
- 发布目标：`v1.6.2`，尚未推送标签或部署容器。版本仍由 Git 标签注入，不修改本地回退版本。
- 本次内容：文件管理内的只读盘内库存、已有季集与海报、库存到整理/STRM 来源选择、整理前后命名对照。保留 v1.6.1 的频道监控/万能转发改进。
- 包含后端、前端源码、已构建 static、测试及说明；不包含账号、运行数据或 Git 元数据。
- 使用说明：[docs/CLOUD_INVENTORY.md](docs/CLOUD_INVENTORY.md)。验证记录：[dev/acceptance-v1.6.2.md](dev/acceptance-v1.6.2.md)。
- NAS 推送脚本：[dev/publish-v1.6.2-nas.sh](dev/publish-v1.6.2-nas.sh)。仅在干净的 main/v1.6.1 基线上覆盖源码、提交并原子推送 main 与新标签；不更改部署目录或容器。

解压后的 StrimKeep 目录是源码暂存目录。不要把整个目录嵌套复制到已有仓库内，不要覆盖 `.git`，不要删除实际部署配置、密码及 data。脚本使用清单核验并只清除列明的旧构建资源。
