"""Compatibility wrapper for the installed retrieval evaluation core."""

from lean_weave.benchmark.retrieval import *  # noqa: F401,F403
from lean_weave.benchmark.retrieval import main

if __name__ == "__main__":
    raise SystemExit(main())
