"""Safe YAML IO. MVP project root is the current working directory."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from lean_weave.config.models import AppConfig
from lean_weave.errors import ConfigError, LeanWeaveError
from lean_weave.logging_setup import get_logger

DEFAULT_CONFIG_PATH = Path(".lean-weave/config.yaml")
log = get_logger("config")


@dataclass
class LoadedConfig:
    config: AppConfig
    source: Path | None
    warnings: list[str] = field(default_factory=list)


def resolve_state_dir(config: AppConfig) -> Path:
    try:
        root = Path.cwd().resolve()
        state = (root / config.workflow.state_dir).resolve()
        if not state.is_relative_to(root):
            raise ConfigError("Invalid configuration field: workflow.state_dir (outside project root)")
        return state
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Cannot resolve configuration field: workflow.state_dir") from None


def _config_path(path: Path | None) -> Path:
    try:
        return (path if path is not None else Path.cwd() / DEFAULT_CONFIG_PATH).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Cannot resolve configuration path") from None


def _validate(data: dict, source: Path) -> AppConfig:
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        # Unknown field names are untrusted input too. Never echo them.
        fields = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = error["loc"]
            known = location and location[0] in AppConfig.model_fields
            safe_location = str(location[0]) if known else "<unknown>"
            fields.append(f"{safe_location}: {error['type']}")
        raise ConfigError(f"Invalid configuration {source}: {', '.join(fields)}") from None
    resolve_state_dir(config)
    return config


def load_config(path: Path | None = None, *, required: bool = False) -> LoadedConfig:
    source = _config_path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path is not None or required:
            raise ConfigError(f"Configuration file missing: {source}") from None
        message = "Configuration file absent; using defaults"
        log.warning("[Config.load] %s", message)
        config = AppConfig()
        resolve_state_dir(config)
        log.info("[Config.load] source=defaults path=%s level=%s", source, config.logging.level)
        return LoadedConfig(config, None, [message])
    except (OSError, UnicodeError, ValueError):
        raise ConfigError(f"Cannot read configuration: {source}") from None
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        position = f" line={mark.line + 1} column={mark.column + 1}" if mark else ""
        log.error("[Config.load] invalid YAML path=%s%s", source, position)
        log.debug("[FIX] YAML parser failure converted to ConfigError")
        raise ConfigError(f"Invalid YAML: {source}{position}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a mapping: {source}")
    try:
        config = _validate(data, source)
    except ConfigError:
        log.error("[Config.load] validation failed path=%s", source)
        raise
    log.debug("[Config.load] path=%s keys=%s", source, sorted(set(data) & AppConfig.model_fields.keys()))
    log.info("[Config.load] source=%s path=%s level=%s", source, source, config.logging.level)
    return LoadedConfig(config, source)


def dump_config(config: AppConfig) -> str:
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)


def init_config(path: Path | None = None, force: bool = False) -> Path:
    source = _config_path(path)
    try:
        if source.is_dir():
            raise LeanWeaveError(f"Configuration target is a directory: {source}")
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open("w" if force else "x", encoding="utf-8") as stream:
            stream.write(
                "# lean-weave product configuration (project root: current working directory).\n"
                "# Secrets are environment-only; api_key_env names a variable, not a key.\n"
                + dump_config(AppConfig())
            )
    except FileExistsError:
        raise LeanWeaveError(f"Configuration exists; use --force to replace: {source}") from None
    except (OSError, UnicodeError, ValueError):
        raise LeanWeaveError(f"Cannot write configuration: {source}") from None
    log.info("[Config.init] path=%s force=%s", source, force)
    return source
