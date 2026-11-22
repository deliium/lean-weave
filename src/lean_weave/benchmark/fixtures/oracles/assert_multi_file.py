#!/usr/bin/env python3
"""Data-only oracle: VERSION is 1.0.0 and banner mentions v."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    root = pathlib.Path(sys.argv[1])
    config = root / "src" / "config.py"
    app = root / "src" / "app.py"
    try:
        config_text = config.read_text(encoding="utf-8")
        app_text = app.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"src/config.py:1:1: E999 {exc}")
        return 1
    if 'VERSION = "1.0.0"' not in config_text:
        print('src/config.py:1:1: E999 VERSION must be "1.0.0"')
        return 1
    if "v" not in app_text.lower() or "VERSION" not in app_text:
        print("src/app.py:1:1: E999 banner must include a version label")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
