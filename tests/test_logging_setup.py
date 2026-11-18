import importlib
import io
import json
import logging

import pytest

from lean_weave.logging_setup import configure_logging, get_logger, resolve_log_level
from lean_weave.schemas import models


@pytest.fixture(autouse=True)
def restore_logging():
    logger = logging.getLogger("lean_weave")
    previous = logger.handlers[:], logger.level, logger.propagate, logging.root.level
    yield
    for handler in logger.handlers:
        if handler not in previous[0]:
            handler.close()
    logger.handlers[:] = previous[0]
    logger.setLevel(previous[1])
    logger.propagate = previous[2]
    logging.root.setLevel(previous[3])


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_redaction_and_idempotence(monkeypatch, fmt):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "default-sensitive-value")
    monkeypatch.setenv("CUSTOM_KEY", "custom-sensitive-value")
    stream = io.StringIO()
    for _ in range(2):
        configure_logging("DEBUG", fmt, stream, secret_env_names=("DEEPSEEK_API_KEY", "CUSTOM_KEY"))
    stream.seek(0)
    stream.truncate()
    logger = get_logger("test")
    logger.info("api_key=%s password=%s", "unconfigured-api-value", "unconfigured-password")
    logger.info("Authorization: Bearer unconfigured-bearer-value")
    before_exception = stream.getvalue()
    assert "unconfigured-api-value" not in before_exception
    assert "unconfigured-password" not in before_exception
    assert "unconfigured-bearer-value" not in before_exception
    stream.seek(0)
    stream.truncate()
    try:
        raise ValueError("custom-sensitive-value")
    except ValueError:
        logger.exception(
            "message %s %s", "default-sensitive-value", {"password": "hidden-value"},
            extra={"details": [{"token": "nested-hidden"}], "custom": "custom-sensitive-value"},
        )
    output = stream.getvalue()
    for secret in ("default-sensitive-value", "custom-sensitive-value", "hidden-value", "nested-hidden"):
        assert secret not in output
    assert "[REDACTED]" in output
    if fmt == "json":
        assert json.loads(output)["level"] == "ERROR"
    else:
        assert output.count("ERROR lean_weave.test") == 1


def test_resolve_precedence(monkeypatch):
    monkeypatch.setenv("LEAN_WEAVE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert resolve_log_level() == logging.DEBUG
    assert resolve_log_level("WARNING") == logging.WARNING
    assert resolve_log_level("invalid") == logging.INFO
    monkeypatch.delenv("LEAN_WEAVE_LOG_LEVEL")
    assert resolve_log_level() == logging.ERROR
    monkeypatch.delenv("LOG_LEVEL")
    assert resolve_log_level() == logging.INFO


def test_schema_import_has_no_logging_side_effects(caplog):
    handlers = logging.root.handlers[:]
    level = logging.root.level
    with caplog.at_level(logging.DEBUG):
        importlib.reload(models)
    assert not caplog.records
    assert logging.root.handlers == handlers
    assert logging.root.level == level
