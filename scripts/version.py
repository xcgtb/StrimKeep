"""把版本号收成单一来源：backend/strimkeep/__init__.py。

在此之前镜像 tag 有两处硬编码（docker-compose.yml、scripts/deploy_config.py），
其中一处还被测试钉死在 0.6.5，所以后端/前端升到 0.7.0 后镜像仍然叫 0.6.5。

用法（仓库根目录）：
    python scripts/version.py            # 校验所有落点，不一致退出码 1
    python scripts/version.py 0.7.1      # 改版本：写 __init__，再刷其它落点

落点：__init__.py（唯一来源）、frontend/package.json、frontend/package-lock.json、
      docker-compose.yml 的发布通道（ghcr 发布镜像 + dev 本地镜像）、README.md 首行标题。
scripts/deploy_config.py 不落版本，运行时从 __init__.py 读。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'backend' / 'strimkeep' / '__init__.py'
VERSION_RE = re.compile(r"__version__\s*=\s*'([^']+)'")
IMAGE_RE = re.compile(r"(?m)^(\s*image:\s*strimkeep(?:-dev)?:)(\S+)[ \t]*$")
PUBLISHED_RE = re.compile(r"(?m)^\s*image:\s*ghcr\.io/xcgtb/strimkeep:(\S+)\s*$")
DEV_RE = re.compile(r"(?m)^\s*image:\s*strimkeep:(\S+)\s*$")
README_RE = re.compile(r"(?m)^#\s*(?:\U0001F3AC\s*)?StrimKeep\s+([\w.\-]+)$")
FIELD_RE = re.compile(r'("version"\s*:\s*")([^"]+)(")')

# (文件, 只取前几处 version) —— package-lock 后面全是依赖版本，不能一起动。
PACKAGES = (('frontend/package.json', 1), ('frontend/package-lock.json', 2))


def read_version():
    match = VERSION_RE.search(SOURCE.read_text(encoding='utf-8'))
    if not match:
        raise SystemExit('读不到 backend/strimkeep/__init__.py 里的 __version__')
    return match.group(1)


def _rewrite(path, pattern, repl, write, limit=0):
    text = path.read_text(encoding='utf-8')
    state = {'n': 0}

    def counted(match):
        if limit and state['n'] >= limit:
            return match.group(0)
        state['n'] += 1
        return repl(match)

    updated = pattern.sub(counted, text)
    if write and updated != text:
        path.write_text(updated, encoding='utf-8', newline='\n')
    return text, updated


def compose_drift(version, write=False):
    """默认 compose 跟随 GHCR 发布通道（latest），版本号只落在 dev 之外的元数据里。"""
    drift = []
    published = PUBLISHED_RE.findall((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
    if published != ['latest']:
        drift.append(f'docker-compose.yml 应引用 ghcr.io/xcgtb/strimkeep:latest，实际 {published}')
    dev = DEV_RE.findall((ROOT / 'docker-compose.dev.yml').read_text(encoding='utf-8'))
    if dev != ['dev']:
        drift.append(f'docker-compose.dev.yml 应为 strimkeep:dev，实际 {dev}')
    return drift


def packages_drift(version, write=False):
    drift = []
    for name, limit in PACKAGES:
        path = ROOT / name
        old, _ = _rewrite(path, FIELD_RE, lambda m: m.group(1) + version + m.group(3), write, limit)
        seen = [m.group(2) for m in list(FIELD_RE.finditer(old))[:limit]]
        stale = [item for item in seen if item != version]
        if len(seen) < limit:
            drift.append(f'{name} 的 version 字段少于 {limit} 处')
        elif stale:
            drift.append(f'{name} 的 version 是 {stale}')
    return drift


def readme_drift(version, write=False):
    path = ROOT / 'README.md'
    old, _ = _rewrite(path, README_RE, lambda m: f'# \U0001F3AC StrimKeep {version}', write, 1)
    match = README_RE.search(old)
    if match and match.group(1) != version:
        return [f'README.md 标题是 {match.group(1)}']
    return []


CHECKS = (compose_drift, packages_drift, readme_drift)


def check(version=None):
    version = version or read_version()
    problems = []
    for check_one in CHECKS:
        problems.extend(check_one(version, False))
    return version, problems


def sync(version):
    SOURCE.write_text(VERSION_RE.sub(f"__version__ = '{version}'",
                                     SOURCE.read_text(encoding='utf-8'), count=1),
                      encoding='utf-8', newline='\n')
    for check_one in CHECKS:
        check_one(version, True)
    return check(version)


def main(argv):
    if not argv or argv[0] in ('--check', 'check'):
        version, problems = check()
        if problems:
            print(f'版本不一致（基准 {version}，来源 backend/strimkeep/__init__.py）：')
            for item in problems:
                print('  -', item)
            print(f'修复：python scripts/version.py {version}')
            return 1
        print(f'版本一致：{version}')
        return 0
    version = argv[0].lstrip('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        print('版本号需要 x.y.z 形式')
        return 2
    _, problems = sync(version)
    if problems:
        print('仍有不一致：')
        for item in problems:
            print('  -', item)
        return 1
    print(f'已刷成 {version}：__init__.py、package.json、package-lock.json、'
          f'docker-compose.yml 通道、README.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
