#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
command -v docker >/dev/null || { echo '请先启用飞牛 Docker。'; exit 1; }
# 口令放在 .env（已 gitignore），compose 只引用变量；缺失时先引导生成，不要写死密码。
if [[ ! -f .env ]]; then
  cp -- .env.example .env
  chmod 600 .env
  echo '已生成 .env。请先把 ADMIN_PASSWORD 改成至少 12 位的密码，再重新执行 bash start.sh。'
  exit 1
fi
if ! grep -Eq "^ADMIN_PASSWORD=['\"]?.{12,}['\"]?[[:space:]]*$" .env; then
  echo '.env 里的 ADMIN_PASSWORD 至少 12 位（当前缺失或过短）。' >&2
  exit 1
fi
docker compose config -q
docker compose up -d --build --wait --wait-timeout 90 strimkeep
docker compose ps
