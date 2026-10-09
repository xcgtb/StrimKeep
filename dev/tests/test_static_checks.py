# -*- coding: utf-8 -*-
"""静态自检：防止 engine 拆分后「跨模块裸引用 / 机械替换损坏」再次混入。"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dev/tools'))
import static_check  # noqa: E402


class TestStaticCheck:
    def test_repo_is_clean(self):
        assert static_check.run(ROOT) == []

    def _fake_root(self, code):
        d = Path(tempfile.mkdtemp())
        (d / 'app').mkdir()
        (d / 'app' / 'm.py').write_text(code, encoding='utf-8')
        return d

    def test_detects_bare_cross_module_reference(self):
        d = self._fake_root("def f():\n    return notify_telegram('x')\n")
        try:
            issues = static_check.run(d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        assert any('notify_telegram' in i for i in issues), issues

    def test_detects_eng_inside_string_literal(self):
        d = self._fake_root("def f(x):\n    return x.get('_eng().Tmdb')\n")
        try:
            issues = static_check.run(d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        assert any('_eng()' in i for i in issues), issues

    def test_allows_imports_globals_and_docstrings(self):
        code = ('"""说明里提到 _eng() 没问题"""\nimport os\nX = 1\n'
                'def f():\n    global Y\n    Y = os.sep\n    return X, Y, len([])\n')
        d = self._fake_root(code)
        try:
            issues = static_check.run(d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        assert issues == [], issues
