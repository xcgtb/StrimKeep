"""改动文件 → 该跑哪一组门禁：把"我记得"变成"脚本会报错"。

背景：0.7.67 改了 `native_media_info.py`，但当时的门禁五组里没有任何测试覆盖它，
五组全绿其实证明不了什么。人靠记性核对不可靠，所以做成脚本。

用法：
    python scripts/gate_coverage.py                       # 用 git status 的改动
    python scripts/gate_coverage.py strimkeep/sync.py ... # 指定文件

退出码：0 = 全部有覆盖；1 = 存在"改了但门禁没覆盖"的模块（CI 可直接用）。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'backend'
TESTS = BACKEND / 'tests'
VERIFY = ROOT / 'scripts' / 'verify.sh'


def changed_modules(argv):
    """把改动路径转成 (模块名, 原路径) —— 只关心 strimkeep/ 下的 .py。"""
    paths = argv or [
        line[3:].strip()
        for line in subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
                                   capture_output=True, text=True).stdout.splitlines()
        if len(line) > 3
    ]
    mods = []
    for raw in paths:
        p = Path(raw.replace('\\', '/'))
        if p.suffix != '.py' or 'strimkeep' not in p.parts:
            continue
        if p.name in ('__init__.py',):
            continue
        mods.append((p.stem, raw))
    return mods


def gate_groups():
    """解析 verify.sh：组名 → 该组的测试文件名集合。"""
    groups, current = {}, None
    for line in VERIFY.read_text(encoding='utf-8').splitlines():
        m = re.match(r'\s*run_group\s+"([^"]+)"\s*(.*)', line)
        if m:
            current = m.group(1)
            groups[current] = {Path(t).name for t in m.group(2).split() if t.endswith('.py')}
    return groups


def tests_referencing(module):
    """哪些测试文件真的引用了这个模块。

    三种引用都要认：
    - `from strimkeep.X import ...` / `import strimkeep.X`
    - `from strimkeep import ...` 里带上 X
    - `monkeypatch.setattr('strimkeep.X.foo', ...)` 这类**字符串引用**
      （test_native_media_info.py 就是这么引用的，只匹配 import 会漏判）
    """
    patterns = (
        re.compile(rf'\bfrom\s+strimkeep\.{module}\s+import\b'),
        re.compile(rf'\bfrom\s+strimkeep\s+import\b[^\n]*\b{module}\b'),
        re.compile(rf'\bimport\s+strimkeep\.{module}\b'),
        re.compile(rf'strimkeep\.{module}\b'),
    )
    hits = set()
    for path in TESTS.glob('test_*.py'):
        text = path.read_text(encoding='utf-8', errors='ignore')
        if any(p.search(text) for p in patterns):
            hits.add(path.name)
    return hits


def main():
    mods = changed_modules(sys.argv[1:])
    if not mods:
        print('没有需要检查的改动（backend/strimkeep/*.py 之外的改动不影响门禁覆盖）。')
        return 0
    groups = gate_groups()
    bad = []
    for module, raw in mods:
        tests = tests_referencing(module)
        if not tests:
            print(f'?? {module:<22} 没有任何测试 import 它（{raw}）')
            bad.append(module)
            continue
        covered = {name: sorted(tests & files) for name, files in groups.items() if tests & files}
        if covered:
            for name, files in covered.items():
                print(f'OK {module:<22} 组「{name}」  {", ".join(files)}')
        else:
            print(f'!! {module:<22} 有测试但不在门禁里：{", ".join(sorted(tests))}')
            bad.append(module)
    print()
    if bad:
        print(f'需要处理：{len(bad)} 个模块（补测试或补门禁组）')
        return 1
    print('全部改动都在门禁覆盖范围内。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
