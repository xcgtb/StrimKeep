#!/bin/sh
# StrimKeep 数据备份（SQLite 一致快照 + 主密钥）。
#
# 为什么不能用 cp：数据库跑在 WAL 模式，直接复制 state.db 可能拿到「已提交事务的
# 回滚日志还没合并」的中间状态，恢复后轻则丢最近写入，重则报 database disk image is
# malformed。SQLite 官方推荐的在线备份是 .backup（或 VACUUM INTO），它会拿读锁做一致
# 快照，不需要停服。
#
# 用法：
#   sh scripts/backup.sh                                  # ./data -> ./backups，保留 14 份
#   sh scripts/backup.sh /path/data /path/backups 30      # 指定目录与保留份数
#   SK_BACKUP_DIR=/mnt/backup sh scripts/backup.sh        # 只改备份目录
#
# 退出码：0 成功；1 找不到数据库或备份失败。
set -eu

DATA="${1:-${STRIMKEEP_DATA_DIR:-./data}}"
DEST="${2:-${SK_BACKUP_DIR:-./backups}}"
KEEP="${3:-14}"
DB="$DATA/data/state.db"

[ -f "$DB" ] || { echo "找不到数据库：$DB" >&2; exit 1; }
mkdir -p "$DEST"

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/state-$STAMP.db"
TMP="$OUT.part"

# 有 sqlite3 就用官方 .backup；没有就用 Python 的 backup API（结果等价）。
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$TMP'"
else
    python3 - "$DB" "$TMP" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
    d.execute('PRAGMA journal_mode=DELETE')
    d.commit()
PY
fi

# 备份前先自检：能打开、能读 sqlite_master，才算可用。
python3 - "$TMP" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as c:
    c.execute('SELECT count(*) FROM sqlite_master').fetchone()
PY

mv -f "$TMP" "$OUT"

# 主密钥不备份 = 备份数据里加密的 settings 无法解密，所以一并拷走（权限收紧）。
if [ -f "$DATA/config/master.key" ]; then
    cp -f "$DATA/config/master.key" "$DEST/master-$STAMP.key"
    chmod 600 "$DEST/master-$STAMP.key" 2>/dev/null || true
fi

# 只保留最近 KEEP 份（默认 14 天量级）。
ls -1t "$DEST"/state-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
    rm -f "$old"
done
ls -1t "$DEST"/master-*.key 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
    rm -f "$old"
done

echo "已备份：$OUT"
