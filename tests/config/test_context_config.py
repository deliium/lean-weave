import pytest
from pydantic import ValidationError

from lean_weave.config import AppConfig, ContextConfig
from lean_weave.config.loader import dump_config, load_config


def test_context_defaults():
    context = AppConfig().context
    assert context.stage_prompt_tokens_max == 6000
    assert context.per_file_chars_max == 12000
    assert context.total_code_chars_max == 40000
    assert context.digest_chars_max == 2000
    assert context.retry_excerpt_chars_max == 8000
    assert context.candidate_bytes_max == 131072
    assert context.scan_bytes_max == 8388608
    assert context.visited_entries_max == 20000
    assert context.search_hits_max == 100
    assert context.recent_commits_max == 10
    assert context.exclusions == []


def test_old_yaml_uses_context_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nproject:\n  name: demo\n")
    loaded = load_config(path, required=True)
    assert loaded.config.context.stage_prompt_tokens_max == 6000
    assert loaded.config.context.exclusions == []


def test_context_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(
        "context:\n"
        "  stage_prompt_tokens_max: 1000\n"
        "  exclusions:\n"
        "    - generated/\n"
        "    - '*.tmp'\n"
    )
    loaded = load_config(path, required=True)
    assert loaded.config.context.stage_prompt_tokens_max == 1000
    assert loaded.config.context.exclusions == ["generated/", "*.tmp"]
    dumped = dump_config(loaded.config)
    path.write_text(dumped)
    again = load_config(path, required=True)
    assert again.config.context.exclusions == ["generated/", "*.tmp"]


@pytest.mark.parametrize(
    "payload",
    [
        {"context": {"unknown": 1}},
        {"context": {"stage_prompt_tokens_max": 0}},
        {"context": {"stage_prompt_tokens_max": -1}},
        {"context": {"stage_prompt_tokens_max": "6000"}},
        {"context": {"per_file_chars_max": 0}},
        {"context": {"total_code_chars_max": 0}},
        {"context": {"digest_chars_max": 0}},
        {"context": {"retry_excerpt_chars_max": 0}},
        {"context": {"candidate_bytes_max": 0}},
        {"context": {"scan_bytes_max": 0}},
        {"context": {"visited_entries_max": 0}},
        {"context": {"search_hits_max": 0}},
        {"context": {"recent_commits_max": 0}},
        {"context": {"exclusions": [""]}},
        {"context": {"exclusions": ["/absolute"]}},
        {"context": {"exclusions": ["../escape"]}},
        {"context": {"exclusions": ["bad\x00pattern"]}},
        {"context": {"exclusions": "generated/"}},
    ],
)
def test_invalid_context_config(payload):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_context_config_direct():
    config = ContextConfig(stage_prompt_tokens_max=10, exclusions=["tmp/"])
    assert config.stage_prompt_tokens_max == 10
