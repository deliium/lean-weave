from enum import Enum


class LogFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


class OutputFormat(str, Enum):
    YAML = "yaml"
    JSON = "json"
