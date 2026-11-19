#!/usr/bin/env python3
"""Create a GitHub Release from docs/releases/<version>.md.

Usage:
    GITHUB_TOKEN=... python3 scripts/create_release.py 3.6.57
    python3 scripts/create_release.py 3.6.57 --file docs/releases/3.6.57.md

Token fallback: /root/.git-credentials (same machine that pushes tags).
"""
import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def repository():
    if os.environ.get('GITHUB_REPOSITORY'):
        return os.environ['GITHUB_REPOSITORY']
    remote = subprocess.check_output(
        ['git', 'remote', 'get-url', 'origin'], text=True).strip()
    match = re.search(r'github\.com[:/]([^/]+/[^/.]+)', remote)
    if not match:
        raise SystemExit('无法从 origin 解析 GitHub 仓库，请设置 GITHUB_REPOSITORY。')
    return match.group(1)


def token():
    if os.environ.get('GITHUB_TOKEN'):
        return os.environ['GITHUB_TOKEN']
    try:
        text = Path('/root/.git-credentials').read_text()
    except OSError:
        raise SystemExit('未设置 GITHUB_TOKEN，且找不到 /root/.git-credentials。')
    match = re.search(r'https://[^:]+:([^@]+)@github\.com', text)
    if not match:
        raise SystemExit('/root/.git-credentials 中没有 GitHub token。')
    return match.group(1)


def api(method, url, payload, headers):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('version', help='例如 3.6.57（自动加 v 前缀）')
    parser.add_argument('--file')
    args = parser.parse_args()
    version = args.version.lstrip('v')
    tag = 'v' + version
    notes = Path(args.file or ('docs/releases/%s.md' % version))
    if not notes.exists():
        raise SystemExit('缺少发行说明：%s' % notes)
    repo = repository()
    headers = {'Accept': 'application/vnd.github+json',
               'Authorization': 'Bearer ' + token(),
               'User-Agent': 'strimkeep-release'}
    base = 'https://api.github.com/repos/%s/releases' % repo
    existing = {item['tag_name'] for item in api('GET', base + '?per_page=50', None, headers)}
    if tag in existing:
        raise SystemExit('%s 已有 Release，未重复创建。' % tag)
    created = api('POST', base, {'tag_name': tag, 'name': tag,
                                 'body': notes.read_text(encoding='utf-8'),
                                 'draft': False, 'prerelease': False}, headers)
    print(created['html_url'])


if __name__ == '__main__':
    main()
