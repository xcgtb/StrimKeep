#!/usr/bin/env python3
"""Idempotent install of the additive My Drive UI V2 patch into current StrimKeep worktree."""
from pathlib import Path
import hashlib
import re
import sys

root = Path(__file__).resolve().parents[2]
index = root / 'static/index.html'
css = root / 'static/css/cloud-v2-layout.css'
js = root / 'static/js/cloud-v2-layout.js'
changelog = root / 'CHANGELOG.md'
if not all(p.is_file() for p in (index, css, js)):
    sys.exit('错误：请把压缩包解压到 StrimKeep Git 工作区根目录（static/index.html 所在位置）。')
source = index.read_text(encoding='utf-8')
if '<script defer src="/static/js/cloud-workbench.js?v=' not in source or '<link rel="stylesheet" href="/static/css/cloud-workbench.css?v=' not in source:
    sys.exit('错误：当前 index.html 与 v1.3.6 页面结构不符，未修改任何文件。')
js_digest = hashlib.sha256(js.read_bytes().replace(b'\r\n',b'\n')).hexdigest()[:12]
css_digest = hashlib.sha256(css.read_bytes().replace(b'\r\n',b'\n')).hexdigest()[:12]
css_tag = f'<link rel="stylesheet" href="/static/css/cloud-v2-layout.css?v={css_digest}">'
js_tag = f'<script defer src="/static/js/cloud-v2-layout.js?v={js_digest}"></script>'
# Replace only owned script/css tags; no unrelated asset edits.
source = re.sub(r'<link rel="stylesheet" href="/static/css/cloud-v2-layout.css\?v=[^\"]+">\s*', '', source)
source = re.sub(r'<script defer src="/static/js/cloud-v2-layout.js\?v=[^\"]+"></script>\s*', '', source)
source, count_css = re.subn(r'(<link rel="stylesheet" href="/static/css/cloud-workbench.css\?v=[^\"]+">)',r'\1\n'+css_tag,source,count=1)
source, count_js = re.subn(r'(<script defer src="/static/js/cloud-workbench.js\?v=[^\"]+"></script>)',r'\1\n'+js_tag,source,count=1)
if count_css != 1 or count_js != 1:
    sys.exit('错误：找不到唯一的旧工作台资源入口，未写入。')
# Frontend runtime verification counts deferred assets and must recognize this additive script.
verifier = root / 'dev/tests/verify_frontend_assets.cjs'
if verifier.is_file():
    check = verifier.read_text(encoding='utf-8')
    if "assert.equal(urls.length,18);" in check:
        check = check.replace("assert.equal(urls.length,18);", "assert.equal(urls.length,19);")
        verifier.write_text(check,encoding='utf-8')
    elif "assert.equal(urls.length,19);" not in check:
        sys.exit('错误：前端资源检查结构已变化，未覆盖 index.html，请人工核对。')
index.write_text(source,encoding='utf-8')
if changelog.is_file():
    log = changelog.read_text(encoding='utf-8')
    marker = '- 我的网盘 UI V2（布局补丁）：'
    line = marker+'增加工作概览、资源接收三模式分区、影视整理六工作区；保留原 DOM ID、API 和事件，未修改后端治理。\n'
    if marker not in log:
        log = log.replace('## 未发布\n','## 未发布\n\n'+line,1)
        changelog.write_text(log,encoding='utf-8')
    if '--release' in sys.argv:
        position = sys.argv.index('--release')
        version = sys.argv[position+1].lstrip('v') if len(sys.argv)>position+1 else ''
        if not re.fullmatch(r'\d+\.\d+\.\d+',version):
            sys.exit('用法：python3 dev/tools/apply_my_drive_ui_v2.py --release 1.3.7')
        header = '## '+version+'（2026-10-15）'
        if header not in log:
            log = log.replace(line, '')
            log = log.replace('## 未发布\n', '## 未发布\n\n'+header+'\n\n'+line,1)
            changelog.write_text(log,encoding='utf-8')
        print('已写入发布日志版本：', version)
print('已安装 V2 静态资源，版本哈希:',js_digest,css_digest)
print('只修改静态页面引用与未发布变更日志；原云盘 API、治理和数据不变。')
