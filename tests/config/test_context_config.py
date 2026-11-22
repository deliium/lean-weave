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


def test_packing_defaults_are_legacy_disabled():
    packing = AppConfig().context.packing
    assert packing.cli_default is False
    assert packing.total_tokens == 8192
    assert packing.system_tokens == 1024
    assert packing.task_tokens == 1024
    assert packing.map_tokens == 512
    assert packing.source_tokens == 4096
    assert packing.tool_tokens == 2048
    assert packing.output_tokens == 1024
    assert packing.max_expansions == 2
    assert packing.cumulative_source_tokens == 8192


def test_packing_partial_yaml_keeps_other_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("context:\n  packing:\n    cli_default: true\n")
    loaded = load_config(path, required=True)
    packing = loaded.config.context.packing
    assert packing.cli_default is True
    assert packing.total_tokens == 8192
    assert packing.source_tokens == 4096
    assert loaded.config.context.stage_prompt_tokens_max == 6000


@pytest.mark.parametrize(
    "payload",
    [
        {"context": {"packing": {"unknown": 1}}},
        {"context": {"packing": {"total_tokens": 0}}},
        {"context": {"packing": {"total_tokens": 10, "output_tokens": 10}}},
        {"context": {"packing": {"total_tokens": 10, "output_tokens": 11}}},
        {"context": {"packing": {"total_tokens": 100, "output_tokens": 10, "source_tokens": 91}}},
        {"context": {"packing": {"total_tokens": 100, "output_tokens": 10, "source_tokens": 50, "cumulative_source_tokens": 49}}},
        {"context": {"packing": {"max_expansions": 17}}},
        {"context": {"packing": {"output_tokens": 0}}},
        {"context": {"packing": {"cli_default": "true"}}},
        {"context": {"packing": {"total_tokens": 1.5}}},
    ],
)
def test_invalid_packing_config(payload):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_packing_section_caps_are_independent():
    packing = AppConfig.model_validate(
        {
            "context": {
                "packing": {
                    "total_tokens": 5000,
                    "output_tokens": 500,
                    "system_tokens": 0,
                    "map_tokens": 0,
                    "source_tokens": 4000,
                    "task_tokens": 0,
                    "tool_tokens": 0,
                    "cumulative_source_tokens": 4000,
                }
            }
        }
    ).context.packing
    assert packing.system_tokens == 0
    assert packing.map_tokens == 0
    assert packing.source_tokens == 4000
    assert packing.total_tokens == 5000
