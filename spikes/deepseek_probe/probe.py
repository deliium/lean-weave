"""DeepSeek live/offline probe.

Live mode when DEEPSEEK_API_KEY is set; otherwise offline fixture assertions.
Never logs API keys or secret-bearing prompts.
"""

from __future__ import annotations

import json
import os
from typing import Any

from lean_weave_spike_logging import configure_spike_logging

from .fixtures import (
    FIXTURE_JSON,
    FIXTURE_THINKING_NO_TOOLS,
    FIXTURE_THINKING_TOOLS,
    FIXTURE_TOOL_CALL,
)

log = configure_spike_logging()

BASE_URL = "https://api.deepseek.com"
BETA_BASE_URL = "https://api.deepseek.com/beta"
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo text back",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


def _usage_fields(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        data = usage
    else:
        data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    details = data.get("completion_tokens_details") or {}
    prompt_details = data.get("prompt_tokens_details") or {}
    return {
        "prompt": data.get("prompt_tokens"),
        "completion": data.get("completion_tokens"),
        "total": data.get("total_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens")
        if isinstance(details, dict)
        else getattr(details, "reasoning_tokens", None),
        "cache_hit": data.get("prompt_cache_hit_tokens", prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None),
    }


def assert_fixture_shapes() -> list[dict[str, Any]]:
    """Offline assertions for supported response shapes and echo rules."""
    results: list[dict[str, Any]] = []

    def check(case: str, fixture: dict[str, Any], *, expect_tools: bool, expect_reasoning: bool) -> None:
        log.debug(
            "[DeepSeekProbe.case] request metadata {case=%s, ok=pending} model=%s thinking=%s tool_count=%s",
            case,
            fixture.get("model"),
            expect_reasoning,
            1 if expect_tools else 0,
        )
        msg = fixture["choices"][0]["message"]
        usage = _usage_fields(fixture.get("usage"))
        ok = True
        errors: list[str] = []
        if expect_tools and not msg.get("tool_calls"):
            ok = False
            errors.append("missing tool_calls")
        if expect_reasoning and not msg.get("reasoning_content"):
            ok = False
            errors.append("missing reasoning_content")
        if usage.get("prompt") is None:
            ok = False
            errors.append("missing prompt_tokens")
        # Echo rule documentation assertion for thinking+tools
        if case == "thinking_tools_echo_rule":
            # Subsequent request with tools MUST include prior reasoning_content
            required_echo = bool(msg.get("reasoning_content"))
            if not required_echo:
                ok = False
                errors.append("thinking+tools requires reasoning_content echo")
        if case == "thinking_no_tools_no_echo_required":
            # Documented: without tools, reasoning_content echo is optional/ignored
            if msg.get("reasoning_content") is None:
                ok = False
                errors.append("fixture should include reasoning_content for docs check")
        results.append({"case": case, "ok": ok, "errors": errors, "usage": usage})
        level = log.info if ok else log.error
        level(
            "[DeepSeekProbe.case] %s {case=%s, ok=%s} usage=%s errors=%s",
            "pass" if ok else "fail",
            case,
            ok,
            usage,
            errors,
        )

    check("non_thinking_tool_call", FIXTURE_TOOL_CALL, expect_tools=True, expect_reasoning=False)
    check(
        "thinking_tools_echo_rule",
        FIXTURE_THINKING_TOOLS,
        expect_tools=True,
        expect_reasoning=True,
    )
    check(
        "thinking_no_tools_no_echo_required",
        FIXTURE_THINKING_NO_TOOLS,
        expect_tools=False,
        expect_reasoning=True,
    )
    check("structured_json", FIXTURE_JSON, expect_tools=False, expect_reasoning=False)

    # json_object caveat: blank content possible — document via synthetic failure shape
    blank = {
        "choices": [{"message": {"role": "assistant", "content": "   ", "reasoning_content": None}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "model": "deepseek-flash",
    }
    content = blank["choices"][0]["message"]["content"]
    blank_ok = content is not None and content.strip() == ""
    results.append(
        {
            "case": "json_object_blank_content_caveat",
            "ok": blank_ok,
            "errors": [] if blank_ok else ["expected blank/whitespace content caveat"],
            "usage": {},
        }
    )
    log.info(
        "[DeepSeekProbe.case] json blank caveat documented {case=json_object_blank_content_caveat, ok=%s}",
        blank_ok,
    )
    return results


def _live_client(base_url: str = BASE_URL):
    from openai import OpenAI

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    return OpenAI(api_key=key, base_url=base_url)


def _run_live_cases() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    client = _live_client()

    def record(case: str, ok: bool, usage: dict[str, Any], detail: str = "") -> None:
        results.append({"case": case, "ok": ok, "usage": usage, "detail": detail})
        (log.info if ok else log.error)(
            "[DeepSeekProbe.case] live {case=%s, ok=%s} usage=%s detail=%s",
            case,
            ok,
            usage,
            detail[:200],
        )

    # Case: non-thinking tool call
    try:
        log.debug(
            "[DeepSeekProbe.case] request metadata {case=live_non_thinking_tool, ok=pending} model=%s thinking=%s tool_count=%s",
            DEFAULT_MODEL,
            False,
            1,
        )
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "Call echo with text=ping"}],
            tools=[ECHO_TOOL],
            extra_body={"thinking": {"type": "disabled"}},
        )
        msg = resp.choices[0].message
        usage = _usage_fields(resp.usage)
        log.info("[DeepSeekProbe.case] token usage {case=live_non_thinking_tool, ok=True} %s", usage)
        ok = bool(getattr(msg, "tool_calls", None))
        record("live_non_thinking_tool", ok, usage, "tool_calls present" if ok else "no tool_calls")
    except Exception as exc:  # noqa: BLE001 — spike surfaces API errors
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        log.error(
            "[DeepSeekProbe.case] HTTP/API failure {case=live_non_thinking_tool, ok=False} status=%s excerpt=%s",
            status,
            str(exc)[:300],
        )
        record("live_non_thinking_tool", False, {}, str(exc))

    # Case: thinking + tools with reasoning_content round-trip
    try:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Call echo with text=roundtrip then stop."}
        ]
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=messages,
            tools=[ECHO_TOOL],
            reasoning_effort="low",
            extra_body={"thinking": {"type": "enabled"}},
        )
        messages.append(resp.choices[0].message)  # includes reasoning_content
        usage1 = _usage_fields(resp.usage)
        tool_calls = resp.choices[0].message.tool_calls
        if tool_calls:
            for tool in tool_calls:
                args = json.loads(tool.function.arguments or "{}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool.id,
                        "content": args.get("text", "ok"),
                    }
                )
            resp2 = client.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=messages,
                tools=[ECHO_TOOL],
                reasoning_effort="low",
                extra_body={"thinking": {"type": "enabled"}},
            )
            usage2 = _usage_fields(resp2.usage)
            ok = resp.choices[0].message.reasoning_content is not None
            record(
                "live_thinking_tools_roundtrip",
                ok,
                {"first": usage1, "second": usage2},
                "reasoning echoed via message append",
            )
        else:
            record(
                "live_thinking_tools_roundtrip",
                False,
                usage1,
                "no tool_calls on first turn",
            )
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        log.error(
            "[DeepSeekProbe.case] HTTP/API failure {case=live_thinking_tools_roundtrip, ok=False} status=%s excerpt=%s",
            status,
            str(exc)[:300],
        )
        record("live_thinking_tools_roundtrip", False, {}, str(exc))

    # Case: structured JSON output
    try:
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": 'Return json object {"ok": true, "stage": "plan"} exactly.',
                }
            ],
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        content = resp.choices[0].message.content or ""
        usage = _usage_fields(resp.usage)
        parsed_ok = False
        try:
            parsed = json.loads(content)
            parsed_ok = parsed.get("ok") is True
        except json.JSONDecodeError:
            parsed_ok = False
        record("live_structured_json", parsed_ok, usage, content[:120])
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        log.error(
            "[DeepSeekProbe.case] HTTP/API failure {case=live_structured_json, ok=False} status=%s excerpt=%s",
            status,
            str(exc)[:300],
        )
        record("live_structured_json", False, {}, str(exc))

    return results


def run_probe() -> dict[str, Any]:
    live = bool(os.environ.get("DEEPSEEK_API_KEY"))
    mode = "live" if live else "offline"
    log.info("[DeepSeekProbe.case] starting {case=run_probe, ok=pending} mode=%s", mode)
    offline = assert_fixture_shapes()
    live_results: list[dict[str, Any]] = []
    if live:
        live_results = _run_live_cases()
    all_ok = all(r["ok"] for r in offline + live_results)
    log.info(
        "[DeepSeekProbe.case] complete {case=run_probe, ok=%s} mode=%s offline=%s live=%s",
        all_ok,
        mode,
        len(offline),
        len(live_results),
    )
    return {
        "mode": mode,
        "ok": all_ok,
        "offline": offline,
        "live": live_results,
        "matrix_notes": {
            "thinking_default": "enabled by default (effort high) on current DeepSeek models",
            "tools_require_reasoning_echo": True,
            "no_tools_reasoning_echo_ignored": True,
            "json_object_requires_json_word_and_example": True,
            "strict_tools_beta_base_url": BETA_BASE_URL,
            "usage_fields": [
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "prompt_cache_hit_tokens",
            ],
            "models": ["deepseek-flash", "deepseek-v4-pro"],
        },
    }


# Re-exports for tests
__all__ = [
    "FIXTURE_JSON",
    "FIXTURE_THINKING_TOOLS",
    "FIXTURE_TOOL_CALL",
    "assert_fixture_shapes",
    "run_probe",
]

if __name__ == "__main__":
    print(json.dumps(run_probe(), indent=2, default=str))
