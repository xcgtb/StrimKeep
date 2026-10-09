#!/usr/bin/env python3
"""Source/release checks: version agreement and private deployment boundaries."""
import argparse
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_DIRS = {'data', 'preview-data', 'state', 'records', '.env', '.venv', 'venv', 'node_modules'}
CACHES = {'__pycache__', '.pytest_cache', '.git'}
PRIVATE_FILES = {'docker-compose.override.yml', 'docker-compose.override.yaml', 'compose.override.yml', 'compose.override.yaml', 'compose.yml', 'compose.yaml'}
BAD_SUFFIXES = ('.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.log', '.jsonl', '.migrated', '.tar', '.tar.gz', '.tgz', '.zip')
SECRET_KEYS = r'WEB_PASSWORD|WEB_TOKEN|SESSION_SECRET|TG_BOT_TOKEN|TMDB_KEY|EMBY_KEY'


def placeholder(value):
    value = value.strip().strip('"').strip("'")
    return not value or value.lower() in {'change-me', 'change-me-please', 'your-secret', 'your-token'} or \
        value.startswith(('CHANGE_ME', '你的', '请改成', '${', '<your-'))


def check(root, archive=False):
    root = Path(root)
    errors = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            path = base / name
            rel = path.relative_to(root)
            if name in PRIVATE_DIRS or (archive and name in CACHES) or path.is_symlink():
                errors.append(f'forbidden directory: {rel}')
                dirs.remove(name)
            elif name in CACHES:
                dirs.remove(name)
        for name in files:
            path = base / name
            rel = path.relative_to(root)
            if path.is_symlink() or name in PRIVATE_FILES or name.endswith(BAD_SUFFIXES) or \
                    (name.startswith('.env') and name != '.env.example') or \
                    (archive and name.endswith(('.pyc', '.pyo'))):
                errors.append(f'forbidden file: {rel}')
                continue
            if path.suffix in ('.yml', '.yaml') and '.github/workflows' in rel.as_posix() and 'docker-compose' in name:
                errors.append(f'compose file is not an Actions workflow: {rel}')
            if name.endswith('.example') or name == 'docker-compose.yml' or rel.as_posix() == 'dev/docker-compose.build.yml':
                text = path.read_text(encoding='utf-8')
                for line in text.splitlines():
                    match = re.match(r'^\s*(' + SECRET_KEYS + r')\s*[:=]\s*(.*?)\s*$', line)
                    if match and not placeholder(match[2]):
                        errors.append(f'non-placeholder credential: {rel}: {match[1]}')
    try:
        version = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", (root/'app/version.py').read_text())[1]
        docker_version = re.search(r'^ARG APP_VERSION=(\S+)', (root/'Dockerfile').read_text(), re.M)[1]
        if version != docker_version: errors.append('Docker/source version mismatch')
        if not (root/'CHANGELOG.md').read_text().startswith('# 版本日志\n\n## '+version+'（'):
            errors.append('changelog release version mismatch')
        for name in ('docker-compose.yml', 'dev/docker-compose.build.yml'):
            compose = (root/name).read_text()
            # 公开部署模板允许 latest；构建模板必须与源码版本一致
            allowed_tags = (
                (version, 'latest')
                if name == 'docker-compose.yml'
                else (version,)
            )
            if not any(
                re.search(
                    r'^\s+image: [^\s]+:' + re.escape(tag) + r'\s*$',
                    compose,
                    re.M,
                )
                for tag in allowed_tags
            ):
                errors.append(f'image/source version mismatch: {name}')
            if 'container_name: strimkeep' not in compose:
                errors.append(f'container name mismatch: {name}')
    except (OSError, TypeError, IndexError):
        errors.append('missing or invalid release metadata')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--archive', action='store_true', help='also reject checkout/build caches')
    args = parser.parse_args()
    errors = check(args.root, args.archive)
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    print('release check: OK; source version, deployment templates and private-file boundaries')


if __name__ == '__main__':
    main()
