#!/usr/bin/env python3
"""Data-only oracle: mark() returns the stamped string."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    path = pathlib.Path(sys.argv[1])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"{path.as_posix()}:1:1: E999 {exc}")
        return 1
    if 'return "stamped"' not in text:
        print(f'{path.as_posix()}:1:1: E999 mark must return "stamped"')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
