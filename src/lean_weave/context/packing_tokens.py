"""Deterministic local token accounting for packed-context composition.

Local counts are always reported as ``local_tokenizer`` or
``conservative_estimate``.  They never masquerade as provider-reported usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from lean_weave.context.packing_models import PackMetrics
from lean_weave.logging_setup import get_logger

log = get_logger("context.packing_tokens")

FRAMING_UNITS = 64
SECTION_NAMES: tuple[str, ...] = ("system", "task", "map", "source", "tool")


class TokenCounterError(RuntimeError):
    """A local counter was unsupported, failed, or returned an invalid count."""


@runtime_checkable
class TokenCounter(Protocol):
    """Compatible local tokenizer adapter.

    Implementations must only claim support for the exact effective model and
    wire shape they were measured against.  ``supports`` is checked before a
    composition run; a failure during counting triggers one conservative
    restart, never a per-section mixture of counters.
    """

    name: str

    def supports(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        response_format: Mapping[str, Any] | None,
        extra_body: Mapping[str, Any] | None,
    ) -> bool: ...

    def count_text(self, text: str) -> int: ...

    def count_request(self, payload: Mapping[str, Any]) -> int: ...


class ConservativeByteCounter:
    """One token per UTF-8 byte plus fixed framing for the whole request."""

    name = "conservative_estimate"

    def supports(self, **_: Any) -> bool:
        return True

    def count_text(self, text: str) -> int:
        return len(text.encode("utf-8"))

    def count_request(self, payload: Mapping[str, Any]) -> int:
        return len(canonical_json(payload).encode("utf-8")) + FRAMING_UNITS


#: The locked runtime ships no verified DeepSeek tokenizer.  Callers may inject
#: a compatible adapter explicitly; an empty registry is the safe default.
DEFAULT_TOKEN_COUNTERS: tuple[TokenCounter, ...] = ()


def canonical_json(value: Any) -> str:
    """Compact, deterministic UTF-8 JSON used for counting and fingerprints."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def supports_counter(
    counter: TokenCounter,
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    response_format: Mapping[str, Any] | None,
    extra_body: Mapping[str, Any] | None,
) -> bool:
    try:
        return bool(
            counter.supports(
                model=model,
                messages=messages,
                tools=tools,
                response_format=response_format,
                extra_body=extra_body,
            )
        )
    except Exception:  # noqa: BLE001 - adapter capability check must fail closed
        return False


def _validate_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenCounterError("token counter returned an invalid count")
    return value


@dataclass(frozen=True)
class TokenMeasurement:
    estimator: str
    section_tokens: dict[str, int]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    remaining_total: int

    def metrics(self, *, total_limit: int) -> PackMetrics:
        return PackMetrics(
            estimator=self.estimator,
            section_tokens=dict(self.section_tokens),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            remaining_total=max(0, total_limit - self.total_tokens),
        )


def measure(
    counter: TokenCounter,
    *,
    sections: Mapping[str, str],
    wire: Mapping[str, Any],
    output_tokens: int,
    total_limit: int,
) -> TokenMeasurement:
    """Count each canonical section and the final composed request separately."""

    counts: dict[str, int] = {}
    for name in SECTION_NAMES:
        counts[name] = _validate_count(counter.count_text(sections.get(name, "")))
    input_tokens = _validate_count(counter.count_request(wire))
    total = input_tokens + int(output_tokens)
    return TokenMeasurement(
        estimator=counter.name,
        section_tokens=counts,
        input_tokens=input_tokens,
        output_tokens=int(output_tokens),
        total_tokens=total,
        remaining_total=max(0, total_limit - total),
    )


def render_user_content(*, task: str, map_rows: Sequence[str], source: str) -> str:
    """Render distinct labeled data blocks; source bytes are never edited."""

    blocks = [
        "<task_specification>\n" + task + "\n</task_specification>",
        "<repository_map>\n" + "\n".join(map_rows) + "\n</repository_map>",
        "<source_fragments>\n" + source + "\n</source_fragments>",
    ]
    return "\n".join(blocks)


def render_fragment_header(path: str, start: int, end: int, digest: str, kind: str) -> str:
    return f"=== {path}:{start}-{end} {digest} {kind} ==="


def render_source(fragments: Sequence[Any]) -> str:
    parts: list[str] = []
    for fragment in fragments:
        parts.append(
            render_fragment_header(
                fragment.path,
                fragment.start_line,
                fragment.end_line,
                fragment.digest,
                fragment.kind,
            )
        )
        parts.append(fragment.text)
    return "\n".join(parts)


def tool_section_text(
    *,
    tools: Sequence[Mapping[str, Any]] | None,
    replay: Sequence[Mapping[str, Any]],
    response_format: Mapping[str, Any] | None,
) -> str:
    if not tools and not replay and response_format is None:
        return ""
    return canonical_json(
        {
            "tools": list(tools or ()),
            "replay": list(replay),
            "response_format": response_format,
        }
    )
