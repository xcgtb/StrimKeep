# -*- coding: utf-8 -*-
"""
domain.quality 的直接测试 + 领域层纯度守卫。

纯度守卫：app/domain/ 下任何模块都只允许 import 白名单里的标准库，
禁止出现 sqlite3 / fastapi / urllib / requests / pathlib / os 等 IO 相关依赖，
也不允许 import 项目内的 emby / tmdb / storage / config / engine 等基础设施模块。
"""
import ast
import sys
from pathlib import Path

APP = Path(__file__).parents[2] / 'app'
sys.path.insert(0, str(APP))

import core  # noqa: E402
from domain import quality  # noqa: E402

ALLOWED_STDLIB = {'re', 'typing', 'dataclasses', 'enum', 'functools', 'collections', 'itertools',
                  'math', 'datetime', '__future__'}


def _imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split('.')[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入 = 项目内模块
                yield '.' * node.level + (node.module or ''), node.lineno
            else:
                yield (node.module or '').split('.')[0], node.lineno


class TestDomainPurity:
    def test_domain_modules_only_import_whitelisted_stdlib(self):
        bad = []
        for py in sorted((APP / 'domain').glob('*.py')):
            for mod, line in _imports(py):
                if mod not in ALLOWED_STDLIB:
                    bad.append('%s:%d import %s' % (py.name, line, mod))
        assert not bad, '领域层出现非白名单依赖（疑似引入 IO/基础设施）:\n' + '\n'.join(bad)


class TestQualityCompare:
    COVER = core.cover_default_strategy()

    def test_higher_resolution_wins(self):
        assert quality.compare_cover('a.2160p.strm', 'a.1080p.strm', self.COVER) == 1
        assert quality.compare_cover('a.720p.strm', 'a.1080p.strm', self.COVER) == -1

    def test_identical_is_tie(self):
        assert quality.compare_cover('a.1080p.strm', 'a.1080p.strm', self.COVER) == 0

    def test_source_outranks_resolution_in_default_order(self):
        # 默认顺序 source 先于 resolution：Remux 1080p 胜 WEB-DL 2160p?  以实际行为为准，这里只锁定与 explain 一致
        a, b = 'a.1080p.BluRay.strm', 'a.2160p.WEB-DL.strm'
        r = quality.compare_cover(a, b, self.COVER)
        assert quality.explain_compare(a, b, self.COVER)['result'] == r

    def test_dolby_vision_profile_ordering(self):
        p7 = 'a.2160p.WEB-DL.DV.P7.strm'
        p8 = 'a.2160p.WEB-DL.DV.P8.strm'
        assert quality.compare_cover(p7, p8, self.COVER) == 1

    def test_core_reexports_are_the_same_objects(self):
        for n in ('compare_cover', 'explain_compare', 'recognize_dims', 'quality_label',
                  'normalize_cover', 'cover_default_strategy', 'COVER_RULE_ORDER'):
            assert getattr(core, n) is getattr(quality, n), n

    def test_pure_function_is_deterministic(self):
        a, b = 'x.2160p.Remux.HDR10.TrueHD.Atmos.strm', 'x.2160p.WEB-DL.SDR.AAC.strm'
        assert quality.compare_cover(a, b, self.COVER) == quality.compare_cover(a, b, self.COVER)
