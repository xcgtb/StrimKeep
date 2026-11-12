# 备份与恢复

数据库是单个 SQLite 文件（`data/data/state.db`，WAL 模式），`config/master.key` 用来解密
其中的敏感设置（播放地址、cookie、115 凭据等）。**两者必须成对备份**，只备份 `.db` 恢复后
加密字段读不出来。

## 备份

```sh
# 仓库根目录；默认 ./data -> ./backups，保留 14 份
sh scripts/backup.sh
# 指定目录与保留份数
sh scripts/backup.sh /path/to/StrimKeep/data /mnt/usb/sk-backup 30
```

脚本行为：

1. 用 `sqlite3 .backup`（无 CLI 时退回 Python 的 `backup()` API）生成一致快照，
   **不停服、不复制 WAL/SHM**；
2. 写完先自检「能打开、能读 `sqlite_master`」，再改名成正式文件（避免半成品被当成备份）；
3. 连同 `config/master.key` 一起拷走（权限 600）；
4. 只保留最近 N 份。

> 不要用 `cp state.db`：WAL 模式下可能拿到未合并提交的中间状态，恢复后可能
> `database disk image is malformed`。

## 恢复

```sh
docker compose stop strimkeep                      # 先停，避免写冲突
cp backups/state-<时间>.db data/data/state.db      # 覆盖
cp backups/master-<时间>.key data/config/master.key
rm -f data/data/state.db-wal data/data/state.db-shm # 残留日志必须清掉
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready          # 期望 version 与数据一致、status=ready
```

恢复后检查三件事：`/health/ready` 为 `ready`、任务中心能看到历史任务、日志中心有恢复前的事件。

## 建议节奏

- 每日一次 `scripts/backup.sh`（cron 或宿主机定时任务），保留 14–30 份；
- 升级前额外手动执行一次（`scripts/deploy.sh` 的 `SK_BACKUP` 可指到本脚本）；
- 每季度做一次**真实恢复演练**（拷到临时目录起一个容器验证），否则备份等于没有。
