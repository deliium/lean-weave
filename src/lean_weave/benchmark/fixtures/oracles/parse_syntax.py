#!/usr/bin/env python3
"""Data-only syntax gate used by the verify-then-repair fixture."""

from __future__ import annotations

import ast
import pathlib
import sys


def main() -> int:
    path = pathlib.Path(sys.argv[1])
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"{path.as_posix()}:{exc.lineno or 1}:1: E999 syntax error")
        return 1
    except (OSError, UnicodeError) as exc:
        print(f"{path.as_posix()}:1:1: E999 {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
