"""Shared logging helpers for architecture spikes."""

from __future__ import annotations

import logging
import os


def configure_spike_logging(default: str = "INFO") -> logging.Logger:
    level_name = (
        os.environ.get("LEAN_WEAVE_LOG_LEVEL")
        or os.environ.get("LOG_LEVEL")
        or default
    ).upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(levelname)s %(message)s",
        )
    else:
        root.setLevel(level)
    return logging.getLogger("lean_weave.spikes")
