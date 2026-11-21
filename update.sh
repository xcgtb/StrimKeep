#!/usr/bin/env bash
set -euo pipefail
package_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -s "$package_root/frontend/package-lock.json" || ! -s "$package_root/frontend/src/main.ts" ]]; then
  echo '更新包缺少前端源码；镜像内构建需要 frontend/src 与 frontend/package-lock.json。' >&2
  exit 1
fi
target_project="${1:-}"
if [[ "$target_project" != /* || ! -f "$target_project/docker-compose.yml" ]]; then
  echo '用法：bash update.sh /旧版StrimKeep项目的绝对路径；该目录必须包含现有 docker-compose.yml。' >&2
  exit 1
fi
target_project="$(cd -- "$target_project" && pwd)"
# Keep env_file and variable references intact when exporting the Compose model.
private_dir="$(mktemp -d "$target_project/.deploy-XXXXXX")"
chmod 700 "$private_dir"
trap 'rm -rf -- "$private_dir"' EXIT
(cd -- "$target_project"; docker compose config --no-interpolate --no-path-resolution --format json) > "$private_dir/current.json"
chmod 600 "$private_dir/current.json"
if [[ -f "$target_project/.env" ]]; then
  cp -- "$target_project/.env" "$private_dir/.env"
  chmod 600 "$private_dir/.env"
fi
if command -v python3 >/dev/null; then
  python3 "$package_root/scripts/deploy_config.py" --preserve-interpolation "$private_dir/current.json" "$private_dir/docker-compose.yml" "$private_dir/.env"
else
  current_image="$(cd -- "$target_project"; docker compose images -q strimkeep | head -n 1)"
  [[ -n "$current_image" ]] || { echo '无法找到旧版运行镜像，请先启动旧版容器，或在 NAS 安装 python3 后重试。' >&2; exit 1; }
  docker run --rm --entrypoint python -v "$private_dir:/migration" -v "$package_root/scripts:/helpers:ro" -v "$package_root/backend/strimkeep:/backend/strimkeep:ro" "$current_image" /helpers/deploy_config.py --preserve-interpolation /migration/current.json /migration/docker-compose.yml /migration/.env
fi
if [[ -f "$private_dir/.env" ]]; then
  docker compose --env-file "$private_dir/.env" --project-directory "$target_project" -f "$private_dir/docker-compose.yml" config -q
else
  docker compose --project-directory "$target_project" -f "$private_dir/docker-compose.yml" config -q
fi
if [[ "$target_project" != "$package_root" ]]; then
  # Copy only shipped program files. Leave runtime data in place.
  for item in backend/strimkeep backend/tests frontend/src frontend/tests docs scripts; do
    if [[ -L "$target_project/$item" ]]; then
      echo "程序目录不可为软链接：$item" >&2; exit 1
    fi
    rm -rf -- "$target_project/$item"
    mkdir -p -- "$target_project/$item"
    cp -R -- "$package_root/$item/." "$target_project/$item/"
  done
  # 前端改由镜像内构建：旧产物只在发布包确实带有副本时同步，不再作为构建前提。
  if [[ -d "$package_root/frontend/dist" ]]; then
    rm -rf -- "$target_project/frontend/dist"
    mkdir -p -- "$target_project/frontend/dist"
    cp -R -- "$package_root/frontend/dist/." "$target_project/frontend/dist/"
  fi
  for item in Dockerfile start.sh update.sh .dockerignore .gitignore README.md SHA256SUMS backend/requirements.in backend/requirements.lock backend/requirements-dev.txt backend/pytest.ini frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/vite.config.ts frontend/index.html; do
    # 开发仓库没有发布包才有的 SHA256SUMS：缺文件就跳过并提示，不能让整次更新中止。
    # （0.9.48 就是从开发仓库直接跑 update.sh，栽在这一行、还没覆盖 compose 就退出了。）
    if [[ ! -f "$package_root/$item" ]]; then
      printf '!! 跳过不存在的文件：%s（开发仓库正常，发布包应当齐全）\n' "$item"
      continue
    fi
    cp -- "$package_root/$item" "$target_project/$item"
  done
fi
if [[ -f "$private_dir/.env" ]] && ! cmp -s "$private_dir/.env" "$target_project/.env"; then
  chmod 600 "$private_dir/.env"
  mv -f -- "$private_dir/.env" "$target_project/.env"
fi
cp -- "$private_dir/docker-compose.yml" "$target_project/docker-compose.yml"
chmod 600 "$target_project/docker-compose.yml"
rm -rf -- "$private_dir"
# Remove known shipped backup copies of the retired modules. Keep unrelated local patches.
if [[ ! -L "$target_project/local-patches" ]]; then
  for retired_backup in before-mapping-task-fix-20261026-113841 before-package7-fix-20261026-080932 before-strm-shared-settings-20261026-095053; do
    rm -rf -- "$target_project/local-patches/$retired_backup"
  done
fi
# Remove the retired one-off migration module left by older releases.
rm -f -- "$target_project/backend/strimkeep/migration.py"
cd -- "$target_project"
docker compose config -q
docker compose up -d --build --wait --wait-timeout 90 strimkeep
docker compose ps
echo '更新完成。请打开原来的管理入口。'
