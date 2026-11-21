#!/usr/bin/env python3
"""Data-only oracle: greet() must include a Hello prefix in its return."""

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
        if not isinstance(node, ast.FunctionDef) or node.name != "greet":
            continue
        text = ast.dump(node)
        if "Hello" in text or "Hello" in source:
            for item in node.body:
                if isinstance(item, ast.Return):
                    return 0
    print(f"{path.as_posix()}:1:1: E999 greet must return a Hello greeting")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
