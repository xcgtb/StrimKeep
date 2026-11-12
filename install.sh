#!/usr/bin/env bash
# StrimKeep 一键安装：只问三个问题，然后拉镜像启动。
#
#   sh install.sh
#
# 也可以全自动（不交互）：
#   ADMIN_PASSWORD=至少12位 STRIMKEEP_DATA_DIR=/vol1/data STRIMKEEP_STRM_DIR=/vol1/strm sh install.sh
#
# 想在同一台机器上试装、又不影响已有实例：加 STRIMKEEP_CONTAINER_NAME=sk-test
set -eu

cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.install.yml"
[ -f "$COMPOSE_FILE" ] || { echo "缺少 $COMPOSE_FILE（请在项目目录里运行）"; exit 1; }

command -v docker >/dev/null 2>&1 || { echo "没装 docker，请先安装 Docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "需要 docker compose v2（docker compose ...）"; exit 1; }

ask() {  # ask <提示> <默认值> → 输出回答
    local prompt="$1" default="$2" answer=""
    if [ -t 0 ]; then
        printf '%s [%s]: ' "$prompt" "$default" >&2
        read -r answer || true
    fi
    printf '%s' "${answer:-$default}"
}

# ── 1. 密码 ─────────────────────────────────────────────
password="${ADMIN_PASSWORD:-}"
while [ -z "$password" ]; do
    if [ -t 0 ]; then
        printf '设置面板登录密码（至少 12 位，输入时不显示）: ' >&2
        stty -echo 2>/dev/null || true
        read -r password || true
        stty echo 2>/dev/null || true
        printf '\n' >&2
    else
        echo "非交互运行请提供 ADMIN_PASSWORD=<至少12位>"; exit 1
    fi
done
if [ "${#password}" -lt 12 ]; then
    echo "密码太短：至少 12 位"; exit 1
fi

data_dir="${STRIMKEEP_DATA_DIR:-$(ask '数据目录（state.db/密钥/日志）' './data')}"
strm_dir="${STRIMKEEP_STRM_DIR:-$(ask 'STRM 输出目录（挂给 Emby）' './strm')}"

mkdir -p "$data_dir" "$strm_dir"

# ── 2. 写 .env ─────────────────────────────────────────
if [ -f .env ]; then
    backup=".env.bak.$(date +%Y%m%d%H%M%S)"
    cp .env "$backup"
    echo "已有 .env，备份为 $backup"
fi
umask 077
cat > .env <<EOF
# 由 install.sh 生成 $(date '+%Y-%m-%d %H:%M:%S')
ADMIN_PASSWORD=$password
STRIMKEEP_DATA_DIR=$data_dir
STRIMKEEP_STRM_DIR=$strm_dir
EOF

# ── 3. 拉镜像并启动 ────────────────────────────────────
echo "拉取镜像 ghcr.io/xcgtb/strimkeep（约 740MB，首次较慢）…"
docker compose -f "$COMPOSE_FILE" pull
docker compose -f "$COMPOSE_FILE" up -d

# ── 4. 等健康检查 ──────────────────────────────────────
echo "等待服务就绪…"
ready=""
for _ in $(seq 1 45); do
    sleep 2
    if curl -fsS http://127.0.0.1:9000/health/ready >/dev/null 2>&1; then
        ready=1
        break
    fi
done

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
ip="${ip:-127.0.0.1}"
if [ -n "$ready" ]; then
    echo
    echo "✅ 安装完成：http://$ip:9000"
    echo "   用户名：admin（或 .env 里的 ADMIN_USERNAME）"
    echo "   数据目录：$data_dir"
    echo "   STRM 目录：$strm_dir"
    echo
    echo "常用命令："
    echo "  查看日志： docker compose -f $COMPOSE_FILE logs -f"
    echo "  升级：     docker compose -f $COMPOSE_FILE pull && docker compose -f $COMPOSE_FILE up -d"
    echo "  停止：     docker compose -f $COMPOSE_FILE down"
else
    echo "服务还没就绪，看日志：docker compose -f $COMPOSE_FILE logs --tail=50"
    exit 1
fi
