#!/usr/bin/env python3
"""Data-only oracle: marker.py must define VALUE = 2 and parse cleanly."""

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
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "VALUE"
            and isinstance(node.value, ast.Constant)
            and node.value.value == 2
        ):
            return 0
    print(f"{path.as_posix()}:1:1: E999 VALUE must equal 2")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
