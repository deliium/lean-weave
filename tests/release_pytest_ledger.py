"""Pytest plugin that records exact collection and outcome ledgers for release evidence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

log = logging.getLogger("lean_weave.release_pytest_ledger")

_COLLECTION: list[str] = []
_OUTCOMES: dict[str, dict[str, str]] = {}
_COLLECTION_ERRORS: list[str] = []


def pytest_configure(config) -> None:
    log.info("release_pytest_ledger_configured")


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--release-ledger-output",
        action="store",
        default=None,
        help="Write release evidence pytest ledger JSON to this path",
    )


def pytest_collection_finish(session) -> None:
    global _COLLECTION
    _COLLECTION = [item.nodeid for item in session.items]
    seen: set[str] = set()
    for nodeid in _COLLECTION:
        if nodeid in seen:
            raise pytest.UsageError(f"duplicate_nodeid:{nodeid}")
        seen.add(nodeid)
    log.info("collection_finish count=%s", len(_COLLECTION))


def pytest_collectreport(report) -> None:
    if report.failed:
        _COLLECTION_ERRORS.append(str(report.longrepr))


def pytest_runtest_logreport(report) -> None:
    entry = _OUTCOMES.setdefault(report.nodeid, {})
    entry[report.when] = report.outcome
    log.debug(
        "runtest_logreport nodeid=%s when=%s outcome=%s",
        report.nodeid,
        report.when,
        report.outcome,
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    output = getattr(session.config.option, "release_ledger_output", None)
    if not output:
        return
    payload: dict[str, Any] = {
        "schema": "lw-pytest-ledger-v1",
        "exitstatus": int(exitstatus),
        "collection": list(_COLLECTION),
        "collection_errors": list(_COLLECTION_ERRORS),
        "outcomes": dict(_OUTCOMES),
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log.info("ledger_written path=%s count=%s", path.name, len(_COLLECTION))
