"""Strict product configuration, separate from AI Factory tooling."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectConfig(ConfigModel):
    name: str = "lean-weave"


class LLMConfig(ConfigModel):
    provider: Literal["deepseek"] = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    thinking: bool = False
    api_key_env: str = Field(default="DEEPSEEK_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]*$")
    timeout_s: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=1024, gt=0)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("must be an absolute HTTPS URL without control characters")
        try:
            parts = urlsplit(value)
        except ValueError:
            raise ValueError("must be a valid absolute HTTPS URL") from None
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("must be an absolute HTTPS URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("must not contain user information")
        if parts.query or parts.fragment:
            raise ValueError("must not contain a query or fragment")
        try:
            parts.port
        except ValueError:
            raise ValueError("must contain a valid port") from None
        return value


class LoggingConfig(ConfigModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["text", "json"] = "text"


class WorkflowConfig(ConfigModel):
    state_dir: str = ".lean-weave"
    max_retries: int = Field(default=2, ge=0)
    feature_token_budget: int = Field(default=20000, gt=0)

    @field_validator("state_dir")
    @classmethod
    def validate_state_dir(cls, value: str) -> str:
        path = Path(value)
        if not value.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a non-empty relative path without parent segments")
        return value


class ContextConfig(ConfigModel):
    """Strict deterministic-retrieval limits; every cap is a hard maximum."""

    stage_prompt_tokens_max: int = Field(default=6000, gt=0)
    per_file_chars_max: int = Field(default=12000, gt=0)
    total_code_chars_max: int = Field(default=40000, gt=0)
    digest_chars_max: int = Field(default=2000, gt=0)
    retry_excerpt_chars_max: int = Field(default=8000, gt=0)
    candidate_bytes_max: int = Field(default=131072, gt=0)
    scan_bytes_max: int = Field(default=8388608, gt=0)
    visited_entries_max: int = Field(default=20000, gt=0)
    search_hits_max: int = Field(default=100, gt=0)
    recent_commits_max: int = Field(default=10, gt=0, le=1000)
    exclusions: list[str] = Field(default_factory=list)

    @field_validator("exclusions")
    @classmethod
    def validate_exclusions(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("exclusions must be non-empty strings")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("exclusions must not contain control characters")
            candidate = value.strip().replace("\\", "/")
            if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
                raise ValueError("exclusions must be project-relative patterns")
            if ".." in candidate.split("/"):
                raise ValueError("exclusions must not contain parent segments")
            if candidate not in result:
                result.append(candidate)
        return result


class AppConfig(ConfigModel):
    version: Literal[1] = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value
