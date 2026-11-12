# StrimKeep 工作日志（2026-10-26 ~ 2026-10-27）

> 下一个接手的智能体：先看这个文件了解项目全貌和本次改动，再去看代码。
> 项目根：/path/to/StrimKeep/
> 本地副本：Windows C:/Users/xc/WorkBuddy/2026-10-26-23-10-02/sk/
> 容器：strimkeep:0.6.5，端口 9000，测试 549 全过

## 本次改动（按提交顺序）
- 48578cc 深色模式修复 + 扩展名点选标签
- abe13c5 实时日志重做（固定列/降噪/级别/手机适配见 951683a）
- 3ab8df0 ISO 圆盘可用性测试 + 扩展名点选
- 2cb1bdb 分享页卡片布局
- c195ff1 NFO/海报生成（STRM 管理页开关，整理+分享生效）
- ff6d364 中文数字兜底 + 唯一命中年份一致兜底
- 97ab9cd 多级队列（转存优先，整理文件边界让路自动续跑）
- 52f0858 深度删除联动 + 失效 STRM 检测 + 分享存活定时
- c0dc018/5f8662b 深度删除 UI 教程三步化

## 关键设计
- 115 全局串行限流器（读 350ms 写 3s），永不绕过
- 识别链：精确→别名→中文数字→唯一+年份，全失败进未识别
- 多级队列：MLFQ 思想，转存优先，整理让路不抢占
- 深度删除：只回收站不彻底删，四类事件白名单，路径后缀对齐

## ⚠️ 教训
- Windows Git Bash heredoc 把 \n 写成 /n——多行补丁用 Write 工具写脚本文件执行
- MemoryCloud.add_video 是方法不是模块函数
- organized_files 有 job_id NOT NULL；output_registry 有 source NOT NULL

## P0 遗留
- [ ] 自动备份
- [ ] git 推远端

## 快速命令
构建部署：docker compose build strimkeep && docker compose up -d strimkeep
测试：docker run --rm -v "$PWD:/repo" -w /repo/backend strimkeep:0.6.5 sh -lc 'pip install -q pytest==9.1.1 && python -m pytest -q'
健康：curl http://127.0.0.1:9000/health/ready

## 播放架构现状（2026-10-27 用户确认）

**当前在用**：:8097 做 302 → 115 CDN 播放（frp 已映射 8097/12366 到公网）
**备着没启用**：本项目:9000 的 /play 302（frp 已映射 9000 到公网，功能完备但用户选择暂不切换）

**切换方案**（用户准备好后三步完成）：
1. `playback_base` 改成公网地址（frp 服务器 IP:9000 或域名）
2. frp 删 8097/12366，加 8096（Emby UI 公网入口）
3. `docker stop `

**⚠️ 在用户明确说切换之前，不要动 、不要改 playback_base、不要删 frp 条目。**


对照方法、26 条同题样本结论、故意不跟的地方、剩余能力差距，全部写在
[docs/_ALIGNMENT.md](_ALIGNMENT.md)。要点：

- 命名骨架本来就一致，差在细节识别：平台别名、DoVi Profile、HDR10+/DoVi HDR、
  UHD BluRay REMUX、Atmos/DTS-HD MA、中文集号（第12集/第28话/第二季/EP15）。
- 两个纯 bug：`DDP5.1` 里的 p5 让 DoVi P8 变成 P5；`WEB-DL` 尾部被当发布组 `-DL`。
- 新增「识别测试」页面（媒体管理组）+ `POST /api/v1/organize/preview`，只读，
  用来核对规则，别再拿真实任务试错。
- 命名口径变了，下次整理/全量核对会重命名存量 STRM（456 条），属预期。
- 未做：整库 STRM 全量同步的配置、库存缺集/多版本清理、Emby 反代。

## 2026-10-29：版本号收口 + 同步清单复用结案

- 版本号从此只有一个来源：`backend/strimkeep/__init__.py`。`scripts/version.py` 负责刷
  `frontend/package.json`、`package-lock.json`、`docker-compose.yml` 的镜像 tag 和 README 标题，
  `--check` 用于发布前校验，`backend/tests/test_version_consistency.py` 钉住全部落点。
  此前 compose 与 `scripts/deploy_config.py` 各写死一次 0.6.5，其中一处还被 test_config063 断言，
  于是页面显示 0.7.0、镜像一直叫 0.6.5，回滚和排查都分不清在跑哪一版。
- 已按 0.7.0 重建镜像并切换容器，容器内 `sync.py`/`console_api.py`/`organizer.py` 与工作区 md5 一致；
  旧镜像保留标签 `strimkeep:0.7.0-pre20261029` 用于回滚，`strimkeep:0.6.5` 标签已删除。
- 同步清单复用结案（上一轮中断的排查）：瓶颈不在 407 条指纹校验，而在「云端版本号」被整理车道
  合法刷新。空扫描不再失效（只在真动云端、或扫描出待处理文件时失效）之后，静默期定时同步稳定命中：
  #777/#778 各 9 秒（`sync_mode=cached`），完整核对需要 44 秒。
- 诊断用的「版本号被刷新：<调用栈>」事件已摘除；「缓存未命中：<原因>」事件保留，一次同步一条。
- 后端回归 681 项在 NAS 容器内跑通（`strimkeep-test:0.7.0`）。
- 同步节奏按  对齐：`0 0 * * *`（每天 00:00 一次完整同步，它的日志里
  也是 00:00:15 才跑完；先前记的“每小时”是对 `0 0 * * *` 的误读）。整理仍是 5 分钟轮询。

## 2026-10-29 深夜：前端主题刷新回浅色 + Emby 预热

- 症状：切到深色后刷新，页面又变浅色。
- 根因：主题只在浏览器 localStorage 里改，从未回写服务端；而 App.vue 的 loadAppearance()
  每次刷新先读 /console/config，用服务端的 theme（一直是 light）把本机选择盖掉。
- 改法：主题以本机为准。index.html 加首屏前脚本（避免闪白），App.vue 抽出 paintTheme()，
  loadAppearance() 只在本机无记录时才采用服务端主题；切换按钮 toggleTheme() 立刻 PUT /console/config，
  跨设备仍会收敛。服务端外观已同步设为深色。
- 顺带：Emby 每小时后台预热（EmbyPrewarm），首跑 users=1 resume=2 latest=24 libraries=14 failed=0。
- 回归 703 通过。
