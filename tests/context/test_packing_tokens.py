from __future__ import annotations

import json

import pytest

from lean_weave.context.packing_tokens import (
    ConservativeByteCounter,
    TokenCounterError,
    canonical_json,
    measure,
    render_source,
    tool_section_text,
)


class FixedCounter:
    name = "local_tokenizer"

    def __init__(self, *, divisor: int = 1, fail: bool = False) -> None:
        self.divisor = divisor
        self.fail = fail

    def supports(self, **_):
        return True

    def count_text(self, text: str) -> int:
        if self.fail:
            raise RuntimeError("adapter exploded")
        return len(text) // self.divisor

    def count_request(self, payload) -> int:
        if self.fail:
            raise RuntimeError("adapter exploded")
        return len(canonical_json(payload)) // self.divisor


def test_conservative_counter_counts_utf8_bytes_and_framing():
    counter = ConservativeByteCounter()
    text = "héllo λ"
    assert counter.count_text(text) == len(text.encode("utf-8"))
    assert counter.count_request({"a": text}) == len(
        canonical_json({"a": text}).encode("utf-8")
    ) + 64


def test_measure_keeps_sections_and_whole_request_separate():
    counter = FixedCounter()
    sections = {
        "system": "sys",
        "task": "task",
        "map": "map",
        "source": "src",
        "tool": "tool",
    }
    wire = {"messages": [{"role": "user", "content": "x"}]}
    result = measure(
        counter, sections=sections, wire=wire, output_tokens=10, total_limit=1000
    )
    assert result.section_tokens == {name: len(text) for name, text in sections.items()}
    assert result.input_tokens == len(canonical_json(wire))
    assert result.total_tokens == result.input_tokens + 10
    assert result.remaining_total == 1000 - result.total_tokens


def test_unavailable_sections_are_zero():
    result = measure(
        ConservativeByteCounter(),
        sections={"task": "x"},
        wire={"messages": []},
        output_tokens=1,
        total_limit=10,
    )
    assert result.section_tokens["system"] == 0
    assert result.section_tokens["source"] == 0


def test_invalid_counter_count_is_explicit():
    class Broken(FixedCounter):
        def count_text(self, text: str) -> int:
            return -1

    with pytest.raises(TokenCounterError):
        measure(
            Broken(),
            sections={"task": "x"},
            wire={"messages": []},
            output_tokens=1,
            total_limit=10,
        )


def test_tool_section_counts_replay_and_schema_once():
    text = tool_section_text(
        tools=[{"type": "function", "function": {"name": "read"}}],
        replay=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
                "reasoning_content": "r",
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
        response_format={"type": "json_object"},
    )
    decoded = json.loads(text)
    assert decoded["tools"][0]["function"]["name"] == "read"
    assert decoded["replay"][0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert decoded["response_format"] == {"type": "json_object"}


def test_render_source_preserves_text_and_headers():
    class Fragment:
        path = "pkg/a.py"
        start_line = 1
        end_line = 1
        digest = "sha256:x"
        kind = "definition"
        text = "def a():\n    return 'é'\n"

    rendered = render_source([Fragment()])
    assert rendered.startswith("=== pkg/a.py:1-1 sha256:x definition ===")
    assert "return 'é'" in rendered
