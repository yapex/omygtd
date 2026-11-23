"""代码卫生守卫：只判「代码在做什么」（AST），不判「文本里有没有某个词」。

判据不精确 = 假阳性 = 规则失效，所以这里一律走语法树，注释与文档字符串不参与。
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "gtd"
FORMATS = {"json", "md", "png"}
LEGACY_FIELDS = {"waiting", "someday"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported_modules(path: Path) -> set[str]:
    mods = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def _identifiers(path: Path) -> set[str]:
    """所有真实出现的标识符：变量名、属性名、关键字参数名、函数/类名。"""
    names = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _calls(path: Path, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(_tree(path))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == name]


# ---------- 分层边界（依赖倒置 / 单一职责） ----------

def test_cli_has_no_sqlite():
    assert "sqlite3" not in _imported_modules(PKG / "cli.py")


def test_sqlite_only_in_storage_modules():
    """只有本地存储层能 import sqlite3；白名单是显式枚举，新模块加进来会被拦下。"""
    storage = {"inbox.py"}
    others = [p.name for p in PKG.glob("*.py")
              if p.name not in storage and "sqlite3" in _imported_modules(p)]
    assert others == []


def test_render_does_not_import_store_or_sqlite():
    mods = _imported_modules(PKG / "render.py")
    assert not ({"store", "sqlite3"} & mods), mods


def test_storage_has_no_print_and_no_argparse():
    """唯一剩下的存储层（inbox.py）同样不许 print / 解析参数。"""
    assert _calls(PKG / "inbox.py", "print") == []
    assert "argparse" not in _imported_modules(PKG / "inbox.py")


# ---------- 反例：按格式分支 ----------

def test_cli_has_no_format_branch():
    offenders = []
    for node in ast.walk(_tree(PKG / "cli.py")):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        has_format_literal = any(isinstance(s, ast.Constant) and s.value in FORMATS
                                 for s in sides)
        touches_format_name = any(isinstance(s, ast.Name) and "format" in s.id.lower()
                                  or isinstance(s, ast.Name) and s.id.lower() in ("fmt", "kind")
                                  for s in sides)
        if has_format_literal and touches_format_name:
            offenders.append(ast.dump(node)[:80])
    assert offenders == []


# ---------- 旧方案字段清零 ----------

def test_no_legacy_field_names_in_package():
    for path in PKG.glob("*.py"):
        hits = LEGACY_FIELDS & _identifiers(path)
        assert not hits, f"{path.name} 仍含旧字段 {sorted(hits)}"


# ---------- 规模 ----------

def test_functions_under_40_lines():
    offenders = []
    for path in PKG.glob("*.py"):
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                if span > 40:
                    offenders.append(f"{path.name}:{node.name} = {span} 行")
    assert not offenders, offenders


def test_renderers_share_one_public_method():
    classes = {node.name: node for node in ast.walk(_tree(PKG / "render.py"))
               if isinstance(node, ast.ClassDef)}
    for cls in ("Renderer", "JsonRenderer", "MdRenderer", "PngRenderer"):
        public = [m.name for m in classes[cls].body
                  if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")]
        assert public == ["render"], (cls, public)
