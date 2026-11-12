StrimKeep 0.6.0 UI 页面叠加紧急修复（仅适用于已安装 StrimKeep-0.6.0-UI-UX-patch.zip 的实例）

解决问题：上一版编译资源的 Vue 动态页面列表缺少正确的 keyed diff 和动态 style patch flag，导致切换栏目时旧页面不隐藏，部分新栏目无法正常呈现。修复浏览器前进后退 hash 解析。

修改范围：仅 frontend/dist/index.html 与 frontend/dist/assets/ 下一个新 JS 文件。不改任何后端接口、数据库、媒体文件、Docker 配置或账户凭据。

覆盖示例（确保路径准确）：
cd '/path/to/StrimKeep'
cp frontend/dist/index.html ../StrimKeep-ui-before-fix2-index.html
unzip -oq ../StrimKeep-0.6.0-UI-page-stack-hotfix.zip -d .
docker compose up -d --build --no-deps strimkeep

注意必须重新构建容器。Safari 若显示旧页面可关闭该站点标签后重新打开。

验证：在 Chromium 手机视口使用模拟 API 完成媒体总览->115 网盘->影视整理->STRM 管理->115 网盘->任务中心->链接转存->通知分类->系统设置->实时日志->媒体总览连续切换；每次只显示当前工作区。验证 115 输入草稿跨页面保留、浏览器历史返回成功、无前端 JS 异常。未在真实 NAS/115/Emby 环境运行回归测试。
