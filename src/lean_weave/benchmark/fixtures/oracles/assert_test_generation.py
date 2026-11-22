#!/usr/bin/env python3
"""Data-only oracle: tests cover normal and edge cases; production unchanged."""

from __future__ import annotations

import ast
import pathlib
import sys

EXPECTED_PRODUCTION = (
    "def add(left, right):\n"
    "    return left + right\n"
)


def _asserts(node: ast.AST) -> list[ast.Assert]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Assert)]


def _constant(node: ast.AST) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    return None


def _add_call(node: ast.AST) -> tuple[object, object, object] | None:
    if not isinstance(node, ast.Compare):
        return None
    left = node.left
    if not (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "add"
        and len(left.args) == 2
        and len(node.comparators) == 1
    ):
        return None
    first = _constant(left.args[0])
    second = _constant(left.args[1])
    expected = _constant(node.comparators[0])
    if first is None or second is None or expected is None:
        return None
    return (first, second, expected)


def main() -> int:
    root = pathlib.Path(sys.argv[1])
    production = root / "src" / "calc.py"
    tests = root / "tests" / "test_calc.py"
    try:
        production_text = production.read_text(encoding="utf-8")
        test_source = tests.read_text(encoding="utf-8")
        tree = ast.parse(test_source)
    except (OSError, SyntaxError, UnicodeError) as exc:
        print(f"tests/test_calc.py:1:1: E999 {exc}")
        return 1
    if production_text.replace("\r\n", "\n") != EXPECTED_PRODUCTION:
        print("src/calc.py:1:1: E999 production module must stay unchanged")
        return 1
    calls = {
        value
        for assert_node in _asserts(tree)
        for value in [_add_call(assert_node.test)]
        if value is not None
    }
    if (1, 2, 3) not in calls:
        print("tests/test_calc.py:1:1: E999 missing normal-case assertion")
        return 1
    if not any(left == -1 and right == 1 and expected == 0 for left, right, expected in calls):
        print("tests/test_calc.py:1:1: E999 missing edge-case assertion")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
