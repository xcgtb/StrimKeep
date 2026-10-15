# 115 依赖与分发记录

核查日期：2026-10-14。使用安装到干净 Python 3.12 环境的 wheel 文件进行检查，而非只读取 PyPI 页面或 metadata 标签。

| 依赖 | 固定版本 | metadata 的 License | wheel 内实际许可文件 |
| --- | --- | --- | --- |
| p115client | 0.0.9.7.2 | MIT | `LICENSE` 为 MIT，`LICENSE_zh` 为 GPL 第 3 版中文文本 |
| p115cipher | 0.0.6 | MIT | `LICENSE` 为 GPLv3，`LICENSE_zh` 为 GPL 第 3 版中文文本 |
| p115oss | 0.1.2 | MIT | `LICENSE` 为 GPLv3，`LICENSE_zh` 为 GPL 第 3 版中文文本 |

当前证据存在冲突，不能据 metadata 的 MIT 标签认定整个依赖链可以直接闭源分发。项目继续采用 Python 路线；后续闭源版本需要取得依赖作者的许可澄清/授权，或替换这些依赖并重新验证协议实现。把 Python 文件编译成二进制或只关闭 GitHub 仓库不能消除这个未决问题。本记录只陈述检查结果，不替作者决定适用许可。

许可文件摘要：

```text
p115client LICENSE     a39db8dbf3764e00ec5b01613e7ef2afc609345ed7b0933937150cb5c4fdedb7
上述 LICENSE_zh        abe9b2234d30421a04afa14acf7b4d75a4125e65297ca33cc1ef74a6fbd51893
p115cipher LICENSE     8b1ba204bb69a0ade2bfcf65ef294a920f6bb361b317dba43c7ef29d96332b9b
p115oss LICENSE        8b1ba204bb69a0ade2bfcf65ef294a920f6bb361b317dba43c7ef29d96332b9b
```

适配器参考 [p115client](https://github.com/ChenyangGao/p115client) 的公开 SDK 接口与已安装版本方法，未复制 FunPan Go 业务代码。账号、任务、媒体整理、来源存储与页面都由本项目实现。锁文件固定本次验证的运行依赖，pytest/Black 等维护工具不进入生产镜像。httpx 现在是播放反代的运行依赖，因此历史数据库测试不再把它当作只能用于 TestClient 的包。

整理增强（2026-10-15）：Docker 通过 Debian 系统包安装 ffmpeg 提供 ffprobe，不新增 Python 依赖；裸源码运行需自行安装。只使用探测器读取限量视频片段，不做转码。系统包及其所含编解码器遵循发行版构建对应的许可证，不能由本项目 MIT 标签推断其全部依赖许可。探测约束与验收见 [整理增强](MY_DRIVE_ORGANIZE_FFPROBE_ACCEPTANCE.md)。

分享免转存（2026-10-15）：继续使用锁定的 p115client 0.0.9.7.2，不升级依赖。核对已安装 SDK `share_snap` 和 `share_download_url` 实现，并参考[作者接口文档](https://p115client.readthedocs.io/en/latest/reference/module/client.html)。下载参数为 share_code/receive_code/file_id，UA 通过请求 headers 传入，返回 P115URL 后校验文件 ID/大小/可用 SHA1；这与个人文件的 download_url/pickcode 分开，不调用 share_receive。接口自动回归不等同于真实分享/CDN 验证，见[本批验收](MY_DRIVE_SHARE_STRM_ACCEPTANCE.md)。

资源接收与维护（2026-10-15）：核对锁定SDK的 `clouddownload_task_add_url` / `upload_file_init` / `user_points_sign_post` / `recyclebin_list` / `recyclebin_clean` 方法及参数，继续Cookie操作，不引入新依赖或AppID。秒传随机分段挑战无法由前128KiB哈希推导，缺少所需数据时明确停止；云下载为异步请求。[Telegram Bot API](https://core.telegram.org/bots/api#update) 的 channel_post 接入现有长轮询，绑定保存时校验过的实际频道ID；本批Bot模式不提供用户会话/任意频道历史读取。详见[接收与维护验收](MY_DRIVE_RECEIVING_ACCEPTANCE.md)。
