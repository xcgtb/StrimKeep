#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态自检（无第三方依赖），专门防 engine 拆分后「跨模块裸引用」这一类运行时才炸的错误。

检查项：
  1. 未定义的全局名：函数里引用了既不是本模块定义/导入、也不是内置的名字
     （v1.7.5 拆分遗留的 emby_lib_of / Tmdb / notify_telegram 等 NameError 就是这一类，
      而且常被外层 `except` 吞掉，表现为数据静默归零）。
  2. 字符串字面量里混进 `_eng()`：机械替换损坏（如 get('_eng().Tmdb')）。

用法：python3 scripts/static_check.py        （有问题退出码 1）
"""
import ast
import builtins
import symtable
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_DUNDERS = {'__file__', '__name__', '__doc__', '__package__', '__spec__', '__path__'}


def _module_defined(top, tree):
    names = set(dir(builtins)) | MODULE_DUNDERS
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            names.add(sym.get_name())
    # 函数内 `global X` 后赋值，同样定义了模块级 X
    def walk(tab):
        for sym in tab.get_symbols():
            if sym.is_declared_global() and sym.is_assigned():
                names.add(sym.get_name())
        for ch in tab.get_children():
            walk(ch)
    walk(top)
    return names


def _undefined_globals(path):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src, str(path))
    top = symtable.symtable(src, str(path), 'exec')
    defined = _module_defined(top, tree)
    bad = []

    def walk(tab):
        if tab is not top:
            for sym in tab.get_symbols():
                if sym.is_referenced() and sym.is_global() and sym.get_name() not in defined:
                    bad.append((tab.get_name(), sym.get_name()))
        for ch in tab.get_children():
            walk(ch)
    walk(top)
    return bad


def _locate(path, name):
    """尽力给出行号（第一处引用）。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
            return n.lineno
    return 0


def _eng_in_strings(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    out = []
    for n in ast.walk(tree):
        if id(n) in docs:
            continue  # 文档字符串里讲解 _eng() 约定是正常的
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and '_eng()' in n.value:
            out.append(n.lineno)
    return out


def run(root=ROOT):
    issues = []
    for path in sorted(list((Path(root) / 'app').glob('*.py')) + list((Path(root) / 'app' / 'routers').glob('*.py'))):
        rel = path.relative_to(root)
        for scope, name in _undefined_globals(path):
            issues.append('%s:%d 未定义的全局名 %r（位于 %s）' % (rel, _locate(path, name), name, scope))
        for ln in _eng_in_strings(path):
            issues.append('%s:%d 字符串字面量里出现 _eng()（疑似机械替换损坏）' % (rel, ln))
    return issues


if __name__ == '__main__':
    problems = run()
    if problems:
        print('\n'.join(problems))
        sys.exit(1)
    print('static check: OK')
