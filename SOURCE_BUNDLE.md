# 本次完整源码包

- 基线：v1.5.21，Git SHA `658f3654c683802366b72e4f0e78ee6f50e7d703`。
- 本次改动：公开频道监控、免转存分享 STRM、万能命令转发、TG 卡片及 115 失败诊断。详见 CHANGELOG「1.6.0」。
- 包内包含后端、前端源码和已构建 static，不需要再应用旧的 public-web 补丁。
- 飞牛配置与部署说明：[docs/CHANNEL_FORWARDING.md](docs/CHANNEL_FORWARDING.md)。
- 验证记录：[dev/acceptance-channel-forwarding.md](dev/acceptance-channel-forwarding.md)。

此源码用于发布 v1.6.0，镜像可用性以 GitHub Actions 构建结果为准。不要覆盖已有 Git 元数据、私有配置和运行数据；完整覆盖源码后仍需要构建/发布镜像并重建容器。版本从 Git 标签注入，app/version.py 的本地回退值按仓库规则保留。
