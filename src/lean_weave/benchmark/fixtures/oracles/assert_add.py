#!/usr/bin/env python3
"""Data-only oracle: require add() to return a + b (reads file text, no import)."""

from __future__ import annotations

import ast
import pathlib
import sys


def main() -> int:
    path = pathlib.Path(sys.argv[1])
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeError) as exc:
        print(f"{path.as_posix()}:1:1: E999 {exc}")
        return 1
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "add":
            for item in node.body:
                if isinstance(item, ast.Return) and isinstance(item.value, ast.BinOp):
                    if isinstance(item.value.op, ast.Add):
                        return 0
    print(f"{path.as_posix()}:1:1: E999 add must return a + b")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
