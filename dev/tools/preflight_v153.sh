#!/usr/bin/env bash
# Pre-release build gate. Stays on main without creating a Git tag.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [ "$(git branch --show-current)" != main ]; then
  echo 'ERROR: release preparation requires main' >&2
  exit 1
fi
if git show-ref --verify --quiet refs/tags/v1.5.3 || [ -n "$(git ls-remote --tags origin refs/tags/v1.5.3)" ]; then
  echo 'ERROR: v1.5.3 tag already exists' >&2
  exit 1
fi
if [ ! -f frontend/package-lock.json ]; then
  echo 'ERROR: missing frontend lockfile' >&2
  exit 1
fi

# A manifest-bounded temporary source tree ensures no local credentials, NAS
# data or unknown files are made visible to the Node/npm container.
STAGE=$(mktemp -d /tmp/strimkeep-153-XXXXXXXX)
trap 'rm -rf "$STAGE"' EXIT
python3 - "$STAGE" <<'PYSTAGE'
from pathlib import Path
import hashlib, shutil, sys
root = Path.cwd()
output = Path(sys.argv[1])
for entry in (root / 'MANIFEST.sha256').read_text().splitlines():
    digest, name = entry.split('  ', 1)
    rel = Path(name)
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit('Unsafe manifest path: ' + name)
    src = root / rel
    if src.is_symlink() or not src.is_file():
        raise SystemExit('Invalid source path: ' + name)
    if hashlib.sha256(src.read_bytes()).hexdigest() != digest:
        raise SystemExit('Source digest mismatch: ' + name)
    dest = output / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
PYSTAGE

echo '=== Node 24 / pinned dependencies / Prettier / TS build / Vitest ==='
docker run --rm \
  -v "$STAGE:/work" -w /work/frontend node:24-bookworm-slim \
  sh -euc 'npm ci --no-audit --no-fund; npm run format; npm run format:check; npm test; npm run build; node ../dev/tools/verify_frontend.cjs'

# Prettier updates only source style and operates after all checks succeed.
cp -a "$STAGE/frontend/src/." frontend/src/
# Refresh the source integrity manifest after Prettier updates exact bytes.
python3 - <<'PYMANIFEST'
from pathlib import Path
from hashlib import sha256
root = Path('.')
lines = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path.name == 'MANIFEST.sha256':
        continue
    if any(part in ('.git', 'node_modules', '__pycache__', '.pytest_cache') for part in path.parts):
        continue
    lines.append(f'{sha256(path.read_bytes()).hexdigest()}  {path.as_posix()}\n')
Path('MANIFEST.sha256').write_text(''.join(lines))
PYMANIFEST

echo '=== Python static checks + release metadata ==='
python3 dev/tools/static_check.py
GITHUB_REF_TYPE=tag GITHUB_REF_NAME=v1.5.3 python3 dev/tools/release_check.py
python3 -m compileall -q app dev/tests/test_storage.py

git diff --check

echo '=== Preflight passed. Commit and push main, run Actions/checks manually BEFORE tagging ==='
echo '=== No tag created, no Docker production image or NAS media touched ==='
