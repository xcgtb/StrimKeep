# 0.2.2 构建修正：NAS 直接使用编译后的前端

用户在更新 0.2.2 时，Docker 的 Node 24 构建阶段执行 `npm ci --no-audit --no-fund` 出现 `Illegal instruction (core dumped)`，退出码 132。能确认进程因非法指令退出，但没有 CPU、内核和运行时诊断，不能将具体原因认定为 CPU 不支持 Node 24；同机此前构建过相同系列镜像。

发布包已包含通过 TypeScript/Vite 编译的 frontend/dist。修正版 Dockerfile 直接将该目录复制到 /app/static，并用 Python 核实 index.html 引用的资源均存在。删除 .dockerignore 对 frontend/dist 的排除规则。更新脚本预先检查前端首页是否存在。Docker 构建完全不再运行 Node/npm。

应用行为和版本保持 0.2.2；不修改数据库结构、账号、Cookie、Token、数据挂载、Emby 或 。NFO/图片 Web 开关继续留待后续，尚未加入。

## 部署

将修正版解压至 `/path/to/StrimKeep-v0.2.2-buildfix`，然后：

```bash
cd '/path/to/StrimKeep-v0.2.2-buildfix/StrimKeep' &&
bash update.sh '/path/to/StrimKeep-v0.1.0-phase1/StrimKeep'
```

脚本将修正后的程序文件复制到原运行目录，保留原 .env，然后只构建、更新 strimkeep。上一轮在镜像构建阶段失败，截图没有出现容器替换完成；需要以当前 docker compose ps 的实际状态为准，不用删除原容器、数据或缓存。

本次构建日志应不再出现 frontend 的 npm ci/npm run build 步骤。更新结束后确认容器 healthy，页面版本 0.2.2。

## 本地验证

前端沿用上一轮已通过编译的相同文件，业务代码沿用已通过 52 项后端测试的 0.2.2。对前端资源校验命令、真实应用静态文件响应、更新脚本语法和模拟 Docker 更新流程进行验证。制作环境没有 Docker，因此不能宣称修正版已在 NAS 构建成功。
