"""Application-owned stderr logging and last-mile secret redaction."""

import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import IO, Literal

_SENSITIVE = re.compile(r"api[_-]?key|token|secret|authorization|password", re.I)
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(
        name if name == "lean_weave" or name.startswith("lean_weave.") else f"lean_weave.{name}"
    )


def resolve_log_level(explicit: str | None = None) -> int:
    selected = (
        explicit if explicit is not None else
        os.getenv("LEAN_WEAVE_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
    )
    level = logging.getLevelNamesMapping().get(selected.upper())
    if not isinstance(level, int):
        get_logger("logging").warning("Invalid logging level; falling back to INFO")
        return logging.INFO
    return level


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secret_env_names: Sequence[str] = ("DEEPSEEK_API_KEY",)):
        super().__init__()
        self.secret_env_names = tuple(secret_env_names)

    def redact(self, value):
        if isinstance(value, Mapping):
            return {
                key: "[REDACTED]" if _SENSITIVE.search(str(key)) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return type(value)(self.redact(item) for item in value)
        if not isinstance(value, str):
            return value
        for name in self.secret_env_names:
            secret = os.getenv(name)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)(authorization[\"']?\s*[:=]\s*)bearer\s+[^\s,;}]+",
            r"\1[REDACTED]", value,
        )
        return re.sub(
            r"(?i)((?:api[_-]?key|token|secret|authorization|password)[\"']?\s*[:=]\s*)"
            r"(?:[\"'][^\"']*[\"']|[^\s,;}]+)",
            r"\1[REDACTED]", value,
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            record.msg = str(self.redact(record.msg))
        record.args = self.redact(record.args)
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        for key in record.__dict__.keys() - _STANDARD:
            record.__dict__[key] = (
                "[REDACTED]" if _SENSITIVE.search(key) else self.redact(record.__dict__[key])
            )
        return True


def redact_console_message(message: str) -> str:
    """Protect direct stderr output using default and configured redaction."""
    message = SecretRedactionFilter().redact(message)
    for handler in logging.getLogger("lean_weave").handlers:
        if not getattr(handler, "_lean_weave_owned", False):
            continue
        for filter_ in handler.filters:
            if isinstance(filter_, SecretRedactionFilter):
                message = filter_.redact(message)
    return message


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        result = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = {key: value for key, value in record.__dict__.items() if key not in _STANDARD}
        if record.exc_text:
            extra["exception"] = record.exc_text
        if record.stack_info:
            extra["stack"] = record.stack_info
        if extra:
            result["extra"] = extra
        return json.dumps(result, default=str)


def configure_logging(
    level: str | None = None,
    fmt: Literal["text", "json"] = "text",
    stream: IO[str] | None = None,
    *,
    secret_env_names: Sequence[str] = ("DEEPSEEK_API_KEY",),
) -> None:
    logger = logging.getLogger("lean_weave")
    for handler in logger.handlers[:]:
        if getattr(handler, "_lean_weave_owned", False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler._lean_weave_owned = True
    handler.addFilter(SecretRedactionFilter(secret_env_names))
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    effective = resolve_log_level(level)
    logger.setLevel(effective)
    source = "flag" if level is not None else (
        "LEAN_WEAVE_LOG_LEVEL" if os.getenv("LEAN_WEAVE_LOG_LEVEL") else
        "LOG_LEVEL" if os.getenv("LOG_LEVEL") else "default"
    )
    logging.getLogger().setLevel(logging.WARNING)
    logger.info("[logging.configure] level=%s format=%s source=%s",
                logging.getLevelName(effective), fmt, source)
    logger.debug("[logging.configure] redaction_installed=true source=%s", source)
