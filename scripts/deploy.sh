#!/bin/sh
# 部署入口：先过门禁，再备份，最后上线。
#
# 为什么要这个脚本：手写「先 verify 再 up」靠自觉，忙起来就会跳过。
# 把门禁放在部署路径上，让"没过门禁的镜像"在物理上无法上线。
#
# 用法：
#   sh scripts/deploy.sh strimkeep:0.7.69                 # 只部署（门禁必跑）
#   sh scripts/deploy.sh strimkeep:0.7.69 strimkeep:0.7.68  # 顺带备份当前镜像
#   SKIP_VERIFY=1 sh scripts/deploy.sh ...                # 紧急跳过门禁（会在日志留痕）
#
# 环境变量：
#   SK_BACKUP  备份脚本路径（默认 /tmp/sk-backup.sh，不存在时跳过备份并提示）

set -u

IMAGE="${1:-}"
PREV="${2:-}"
if [ -z "$IMAGE" ]; then
    printf '用法: sh scripts/deploy.sh <新镜像> [回滚镜像]\n'
    exit 2
fi

printf '=== 部署 %s ===\n' "$IMAGE"

if [ "${SKIP_VERIFY:-0}" = "1" ]; then
    printf '!! 已跳过门禁（SKIP_VERIFY=1）——这次上线没有回归验证\n'
else
    sh scripts/verify.sh "$IMAGE" || {
        printf '!! 门禁未通过，拒绝部署 %s\n' "$IMAGE"
        exit 1
    }
fi

BACKUP="${SK_BACKUP:-/tmp/sk-backup.sh}"
if [ -n "$PREV" ]; then
    if [ -x "$BACKUP" ] || [ -f "$BACKUP" ]; then
        # sk-backup.sh 收的是版本号（0.7.68），不是镜像名（strimkeep:0.7.68）——
        # 直接传镜像名会被它再拼一次前缀，变成 strimkeep:strimkeep:0.7.68。
        PREV_VERSION=$(printf '%s' "$PREV" | sed 's/^strimkeep://')
        sh "$BACKUP" "deploy-$(date +%Y%m%d-%H%M%S)" "$PREV_VERSION" || {
            printf '!! 备份失败，拒绝部署（没有退路就不上线）\n'
            exit 1
        }
    else
        printf '!! 找不到备份脚本 %s：已跳过备份，请自行确认可回滚\n' "$BACKUP"
    fi
fi

docker compose up -d || {
    printf '!! docker compose up -d 失败\n'
    exit 1
}

printf '=== 已部署 %s ===\n' "$IMAGE"
docker inspect -f 'image={{.Config.Image}}' strimkeep 2>/dev/null
