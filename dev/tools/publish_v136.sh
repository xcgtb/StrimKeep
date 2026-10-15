#!/usr/bin/env bash
# Run from the NAS Git worktree after extracting the cumulative source package.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if [[ "$(git branch --show-current)" != main ]]; then
  echo '请在 main 分支合并本次累计代码后发布。' >&2
  exit 1
fi
git fetch origin main --tags
if git show-ref --verify --quiet refs/tags/v1.3.6; then
  echo 'v1.3.6 已存在，保留不可变标签，请检查该标签的构建状态。' >&2
  exit 1
fi
if ! git merge-base --is-ancestor origin/main HEAD; then
  echo '远端 main 已更新，请先合并远端改动后重新发布，不强制推送。' >&2
  exit 1
fi
# NAS may only have Git/Docker. Full source tests have passed locally and run again in tag CI.
if command -v python3 >/dev/null 2>&1; then
  python3 dev/tools/release_check.py
  python3 dev/tools/static_check.py
else
  echo 'NAS未安装python3：发布边界和Python检查将由标签CI执行。'
fi
if command -v node >/dev/null 2>&1; then
  node dev/tools/update_frontend_asset_versions.cjs --check
else
  echo 'NAS未安装Node：前端检查将由标签CI执行。'
fi
git diff --check
git add -- app static docs dev README.md CHANGELOG.md Dockerfile requirements-cloud115.lock
git diff --cached --check
if ! git diff --cached --quiet; then
  git commit -m 'release: v1.3.6 频道历史、接收整理和实际入库确认'
fi
if command -v python3 >/dev/null 2>&1; then
  GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.3.6 python3 dev/tools/release_check.py
fi
git tag -a v1.3.6 -m 'StrimKeep v1.3.6'
git push --atomic origin main v1.3.6
