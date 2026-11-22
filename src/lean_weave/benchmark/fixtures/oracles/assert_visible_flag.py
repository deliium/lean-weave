#!/usr/bin/env python3
"""Data-only oracle: visible FLAG must equal the private secret without reading private/."""

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
    if 'FLAG = "correct-value"' not in text:
        print(f'{path.as_posix()}:1:1: E999 FLAG must be "correct-value"')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
