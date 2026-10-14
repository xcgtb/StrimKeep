#!/usr/bin/env python3
"""Apply the StrimKeep 115 P1 UI overlay in a checked-out v1.3.1 source tree.

This changes only the source worktree. It never touches Docker data, credentials,
remote files, media, or backend workflows. Safe to run more than once.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / 'static/index.html'
WORKBENCH = ROOT / 'static/js/cloud-workbench.js'
NEW_JS = ROOT / 'static/js/cloud-workbench-ui.js'
NEW_CSS = ROOT / 'static/css/cloud-workbench-ui.css'
MANIFEST = ROOT / 'MANIFEST.sha256'
OLD_GIT_BLOB = 'b97c18cf6e056f70421552d8cb8c7efbafd091d5'
OLD_RETURN = '  return {load:load};'
NEW_RETURN = '''  // Directory picker bridge. All remote writes remain in the existing workflow.
  function selectDirectory(role, folder, accountId) {
    if (!['source', 'target', 'transfer'].includes(role)) throw Error('目录类型无效');
    if (s.account !== accountId) throw Error('账号已变化，请重新选择目录');
    if (!folder || !/^[0-9]{1,25}$/.test(String(folder.id))) throw Error('目录 ID 无效');
    var next = {id:String(folder.id), name:String(folder.name || '根目录'), trailIds:Array.isArray(folder.trailIds) ? folder.trailIds.map(String) : []};
    var source = role === 'source' ? next : s.source;
    var target = role === 'target' ? next : s.target;
    if (source && target && (source.id === target.id || (target.trailIds || []).includes(source.id))) {
      throw Error('扫描来源与归档目录不能相同，归档目录也不能位于扫描来源内部');
    }
    s.epoch++; s.plan = null; s.planRequest++;
    s[role] = next;
    paths(); msg('已选择' + ({source:'来源', target:'归档', transfer:'转存'}[role]) + '目录：' + s[role].name);
    return s[role];
  }
  return {load:load, selectDirectory:selectDirectory};'''


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def digest(path):
    raw = path.read_bytes()
    if path.suffix.lower() in ('.css', '.js', '.svg'):
        raw = raw.replace(b'\r\n', b'\n')
    return sha_bytes(raw)


def install():
    if not all(p.is_file() for p in (INDEX, WORKBENCH, NEW_JS, NEW_CSS)):
        raise SystemExit('缺少源码或补丁文件，请先把补丁 ZIP 解压到 StrimKeep Git 工作区')
    original = WORKBENCH.read_bytes()
    source = original.decode('utf-8')
    if 'function selectDirectory(role, folder, accountId)' not in source:
        if git_blob(original) != OLD_GIT_BLOB:
            raise SystemExit('工作区 cloud-workbench.js 不属于已核实的 v1.3.1 基线，停止覆盖，请先核对 git status')
        if source.count(OLD_RETURN) != 1:
            raise SystemExit('找不到预期的工作台导出位置，未修改文件')
        source = source.replace(OLD_RETURN, NEW_RETURN)
        WORKBENCH.write_text(source, encoding='utf-8', newline='\n')
        print('已增加工作台目录选择桥接接口')
    else:
        if source.count('function selectDirectory(role, folder, accountId)') != 1:
            raise SystemExit('发现重复桥接接口，停止安装')
        print('工作台桥接接口已存在，跳过')

    html = INDEX.read_text(encoding='utf-8')
    for old, addition in (
        (r'(<link rel="stylesheet" href="/static/css/cloud-workbench\.css\?v=[a-f0-9]{12}">)',
         '<link rel="stylesheet" href="/static/css/cloud-workbench-ui.css?v=PENDING">'),
        (r'(<script defer src="/static/js/cloud-workbench\.js\?v=[a-f0-9]{12}"></script>)',
         '<script defer src="/static/js/cloud-workbench-ui.js?v=PENDING"></script>'),
    ):
        target = re.search(old, html)
        if not target:
            raise SystemExit('找不到 v1.3.1 前端引用标记，停止修改 HTML')
        if addition.split('?')[0] not in html:
            html = html.replace(target.group(0), target.group(0) + '\n' + addition, 1)
    # Correct stale versions for all JS/CSS/SVG references to match existing CI.
    pattern = re.compile(r'(?P<key>(?:src|href)=")(?P<uri>/static/[^"#]+)(?P<end>")')
    def update(match):
        uri = match.group('uri')
        pathname, _, params = uri.partition('?')
        if not params.startswith('v='):
            return match.group(0)
        path = ROOT / pathname.lstrip('/')
        if not path.is_file():
            raise SystemExit(f'静态资源不存在：{pathname}')
        return match.group('key') + pathname + '?v=' + digest(path)[:12] + match.group('end')
    html = pattern.sub(update, html)
    INDEX.write_text(html, encoding='utf-8', newline='\n')
    print('已更新 static/index.html 引用与缓存版本')

    if MANIFEST.is_file():
        old = MANIFEST.read_text(encoding='utf-8').splitlines()
        entries = {}
        for line in old:
            match = re.match(r'^([a-f0-9]{64})  (.+)$', line)
            if match:
                entries[match[2]] = match[1]
        paths = (
            'static/index.html', 'static/js/cloud-workbench.js',
            'static/js/cloud-workbench-ui.js', 'static/css/cloud-workbench-ui.css',
            'dev/tools/apply_cloud_p1.py', 'docs/CLOUD115_P1_UI.md',
            'dev/tests/verify_cloud_p1_ui.cjs',
        )
        for rel in paths:
            path = ROOT / rel
            if path.is_file():
                entries[rel] = sha_bytes(path.read_bytes())
        MANIFEST.write_text(''.join(f'{entries[k]}  {k}\n' for k in sorted(entries)), encoding='utf-8', newline='\n')
        print('已更新 MANIFEST.sha256')
    print('P1 源码补丁安装完成。没有修改任何 Docker 部署数据。')


if __name__ == '__main__':
    if len(sys.argv) != 1:
        raise SystemExit('用法：python3 dev/tools/apply_cloud_p1.py')
    install()
