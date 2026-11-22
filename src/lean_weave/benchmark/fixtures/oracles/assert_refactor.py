#!/usr/bin/env python3
"""Data-only oracle: helper extraction preserves classify behavior structure."""

from __future__ import annotations

import ast
import pathlib
import sys


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _calls(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
        for child in ast.walk(node)
    )


def _calls_method(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == name
        for child in ast.walk(node)
    )


def main() -> int:
    root = pathlib.Path(sys.argv[1])
    path = root / "src" / "labels.py"
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeError) as exc:
        print(f"src/labels.py:1:1: E999 {exc}")
        return 1
    functions = _functions(tree)
    if "_normalize" not in functions:
        print("src/labels.py:1:1: E999 missing _normalize helper")
        return 1
    if "classify" not in functions:
        print("src/labels.py:1:1: E999 missing classify callable")
        return 1
    classify = functions["classify"]
    if not _calls(classify, "_normalize"):
        print("src/labels.py:1:1: E999 classify must call _normalize")
        return 1
    for literal in ('"truthy"', '"other"', '"yes"', '"y"'):
        if literal not in source:
            print(f"src/labels.py:1:1: E999 missing preserved literal {literal}")
            return 1
    normalize = functions["_normalize"]
    if not _calls_method(normalize, "strip") or not _calls_method(normalize, "lower"):
        print("src/labels.py:1:1: E999 _normalize must strip and lower")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
