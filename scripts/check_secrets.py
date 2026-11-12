"""扫一遍 git 跟踪的文件，确认没有明文口令 / 令牌 / API Key。

用法：
    python scripts/check_secrets.py            # 扫全部跟踪文件
    python scripts/check_secrets.py --staged   # 只看已暂存内容（pre-commit 用）

退出码 1 表示命中，命中行会把疑似秘密打码后再打印。
故意不扫未跟踪文件（`.env` 本来就该在本地且已被 gitignore）。
"""
import re
import subprocess
import sys

SUFFIXES = ('.py', '.sh', '.yml', '.yaml', '.json', '.md', '.ts', '.js', '.vue',
            '.ini', '.toml', '.cfg', '.txt', '.html')
SKIP = ('package-lock.json',)

PATTERNS = (
    # password = "..." / token: '...' / api_key: "..." 这类赋值
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
               r"\s*[:=]\s*['\"]([^'\"\s]{6,})['\"]"),
    # 常见口令形态：纯字母 + 长数字串
    re.compile(r"\b(?:zzx|admin|root)\d{6,}\b"),
    # 第三方风格的长随机串
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)

# 明显是占位符 / 测试值，不算泄露
PLACEHOLDER = ('test-', 'acc-password', 'placeholder', 'example', 'changeme',
               'dummy', 'your-', 'xxxx', '****', '${', '{{', '<', '…')


def tracked_files():
    out = subprocess.run(['git', 'ls-files', '-z'], capture_output=True, check=True)
    for raw in out.stdout.decode('utf-8', 'replace').split('\0'):
        if raw and raw.endswith(SUFFIXES) and not raw.endswith(SKIP):
            yield raw


def staged_lines(path):
    try:
        out = subprocess.run(['git', 'show', ':' + path], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return []
    return out.stdout.decode('utf-8', 'replace').splitlines()


def working_lines(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def looks_placeholder(value):
    low = value.lower()
    return any(mark in low for mark in PLACEHOLDER)


def min_length(path):
    """测试代码里短凭据几乎都是 fixture；仓库其它地方按更严格的长度判。"""
    normalized = path.replace('\\', '/')
    return 32 if normalized.startswith('tests/') or '/tests/' in normalized else 8


def scan(paths):
    hits = []
    for path in paths:
        floor = min_length(path)
        for number, line in enumerate(paths[path], start=1):
            for pattern in PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                secret = match.group(1) if match.groups() else match.group(0)
                if looks_placeholder(secret) or len(secret) < floor:
                    continue
                masked = secret[:2] + '…' + secret[-2:] if len(secret) > 6 else '…'
                hits.append((path, number, line.strip()[:100].replace(secret, masked)))
                break
    return hits


def main(argv):
    staged = '--staged' in argv
    files = list(tracked_files())
    if staged:
        files = [name for name in files if subprocess.run(
            ['git', 'diff', '--cached', '--name-only', '--', name],
            capture_output=True).stdout.strip()]
    content = {name: (staged_lines(name) if staged else working_lines(name)) for name in files}
    hits = scan(content)
    if not hits:
        print('OK：%d 个跟踪文件里没有明文口令/令牌。' % len(files))
        return 0
    print('发现 %d 处疑似明文凭据：' % len(hits))
    for path, number, snippet in hits:
        print('  %s:%s: %s' % (path, number, snippet))
    print('\n口令/令牌请放进项目目录的 .env（600，已 gitignore），不要写进仓库文件。')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
