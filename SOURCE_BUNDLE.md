# 频道监控与万能转发交互改进源码包（v1.6.1）

- 基线：main / v1.6.0，Git SHA `e412e587b80d8499b16bb80b518f06eb09df15e9`。
- 本次改动：分组配置弹窗、内置/外部接收者选择、最近消息只读检查、直接启停、可读筛选结果及接收历史任务跳转。详见 CHANGELOG「未发布」。
- 包内包含后端、前端源码和已构建 static，不需要再应用旧的 public-web 补丁。
- 飞牛配置与部署说明：[docs/CHANNEL_FORWARDING.md](docs/CHANNEL_FORWARDING.md)。
- 本次验证记录：[dev/acceptance-channel-ux.md](dev/acceptance-channel-ux.md)。

本次发布目标为 v1.6.1；推送标签后构建镜像，运行容器需要另行更新。不要覆盖已有 Git 元数据、私有配置和运行数据；完整覆盖源码后仍需要构建/发布镜像并重建容器。新发布需使用新的不可变标签；不得覆盖 v1.6.0。版本从 Git 标签注入，app/version.py 的本地回退值按仓库规则保留。
