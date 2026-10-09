# -*- coding: utf-8 -*-
"""static/index.html 的 XSS 回归守卫（正则级）。

重点防两类曾经存在的写法：
  1. 内联事件里 `\'' + esc(x) + '\'`：esc 把 ' 转成 &#39;，HTML 解析时又还原成 '，字符串照样被逃逸 → 必须用 jsarg()
  2. toast() 把消息直接拼进 innerHTML → 必须用 textContent
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / 'static'
HTML = (STATIC / 'index.html').read_text(encoding='utf-8')
# Security guards must continue inspecting extracted JS, not only HTML markup.
HTML += '\n' + '\n'.join(p.read_text(encoding='utf-8') for p in sorted((STATIC / 'js').glob('*.js')))


class TestFrontendXss:
    def test_no_esc_inside_inline_js_string(self):
        assert not re.search(r"\\'' \+ esc\(", HTML)

    def test_no_dynamic_value_inside_inline_js_string(self):
        # 允许的只有常量标识符 s[0]（GROUPS 里的固定页签名）
        bad = [m.group(0) for m in re.finditer(r"\\'' \+ ([^+']+?) \+ '\\'", HTML)
               if not re.fullmatch(r"s\[\d\]", m.group(1).strip())]
        assert bad == [], bad

    def test_jsarg_helper_defined(self):
        assert 'function jsarg(' in HTML

    def test_toast_uses_text_content(self):
        m = re.search(r"function toast\(.*?\n\}", HTML, re.S)
        assert m and 'textContent' in m.group(0) and "'<span>' + msg" not in m.group(0)
