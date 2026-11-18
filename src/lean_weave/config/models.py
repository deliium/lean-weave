"""Strict product configuration, separate from AI Factory tooling."""

from pathlib import Path
from typing import Literal

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


class LoggingConfig(ConfigModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["text", "json"] = "text"


class WorkflowConfig(ConfigModel):
    state_dir: str = ".lean-weave"
    max_retries: int = Field(default=2, ge=0)

    @field_validator("state_dir")
    @classmethod
    def validate_state_dir(cls, value: str) -> str:
        path = Path(value)
        if not value.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError("must be a non-empty relative path without parent segments")
        return value


class AppConfig(ConfigModel):
    version: Literal[1] = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)

    @field_validator("version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value
