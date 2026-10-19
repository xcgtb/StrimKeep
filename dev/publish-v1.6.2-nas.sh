#!/usr/bin/env bash
# Run from the extracted source directory; target is the existing NAS Git checkout.
set -euo pipefail
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="${1:-/vol1/1000/git/StrimKeep}"
base_sha='9b9aaeb06104ae184d7c80090f8da37c9802706f'
release_tag='v1.6.2'
cd -- "$repo_dir"
[[ "$(git branch --show-current)" == main ]] || { echo '请先切到 main；未覆盖源码。' >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo 'Git 工作区有未提交内容；请先保存，未覆盖源码。' >&2; exit 1; }
[[ "$(git rev-parse --show-toplevel)" != "$bundle_dir" ]] || { echo '请在仓库外解压源码包。' >&2; exit 1; }
git fetch origin main --tags
[[ "$(git rev-parse HEAD)" == "$base_sha" && "$(git rev-parse origin/main)" == "$base_sha" && "$(git rev-parse 'v1.6.1^{commit}')" == "$base_sha" ]] || { echo '本地或远程不是本包要求的 v1.6.1 基线；未覆盖源码。' >&2; exit 1; }
if git rev-parse --verify --quiet "refs/tags/$release_tag" >/dev/null; then
  echo 'v1.6.2 已存在，不能复用或覆盖标签。' >&2
  exit 1
fi
git var GIT_AUTHOR_IDENT >/dev/null
GITHUB_REF_TYPE=tag GITHUB_REF_NAME="$release_tag" python3 "$bundle_dir/dev/tools/release_check.py" --root "$bundle_dir" --archive
path_list="$(mktemp)"
trap 'rm -f -- "$path_list"' EXIT
python3 - "$bundle_dir" "$repo_dir" "$path_list" <<'PY'
import hashlib
import shutil
import sys
from pathlib import Path, PurePosixPath
source, target, stage = map(Path, sys.argv[1:])
source, target = source.resolve(), target.resolve()
entries = []
for line in (source / 'MANIFEST.sha256').read_text().splitlines():
    digest, name = line.split('  ', 1)
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise SystemExit('清单路径无效')
    if any(p in {'.git','data','state','records','node_modules','.venv','venv'} for p in relative.parts):
        raise SystemExit('清单含受保护目录')
    path = source / name
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit('源码核验失败：' + name)
    entries.append(name)
obsolete = (source / 'dev/obsolete-assets-after-v1.6.1.txt').read_text().splitlines()
for name in obsolete:
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or not name.startswith('static/assets/') or name in entries:
        raise SystemExit('旧资源清单无效：' + name)
# Validate every destination before changing any file.
for name in [*entries, *obsolete, 'MANIFEST.sha256']:
    dest = target / name
    for part in [dest, *dest.parents]:
        if part == target:
            break
        if part.is_symlink():
            raise SystemExit('目标路径含符号链接：' + name)
    if dest.exists() and not dest.is_file():
        raise SystemExit('目标文件被目录占用：' + name)
for name in entries:
    dest = target / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / name, dest)
shutil.copy2(source / 'MANIFEST.sha256', target / 'MANIFEST.sha256')
for name in obsolete:
    (target / name).unlink(missing_ok=True)
stage.write_bytes(b''.join(name.encode() + b'\0' for name in dict.fromkeys([*entries,*obsolete,'MANIFEST.sha256'])))
print('已核验并覆盖源码；只移除明确列出的旧静态资源。')
PY
python3 dev/tools/static_check.py
git add -A --pathspec-from-file="$path_list" --pathspec-file-nul
git diff --cached --check
git diff --cached --stat
git commit -m 'release: v1.6.2 - cloud inventory and organize preview'
git tag -a "$release_tag" -m 'StrimKeep v1.6.2'
git push --atomic origin main "$release_tag"
echo 'main 与 v1.6.2 已推送；请等 GitHub Actions 镜像构建成功后，再更新部署目录中的容器。'
