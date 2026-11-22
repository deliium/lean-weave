"""DeepSeek gateway: one-attempt transport, validation, and accounting."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
import openai
from openai import OpenAI
from pydantic import ValidationError

from lean_weave.config.models import LLMConfig
from lean_weave.contracts._types import (
    PROBE_FEATURE_ID,
    PROBE_STAGE,
    GatewayStage,
    LLMRequest,
    LLMResponse,
    ThinkingMode,
)
from lean_weave.contracts.deepseek_request import (
    MODEL_CAPABILITIES,
    NormalizedChatRequest,
    PackedRequestGuard,
    normalize_chat_request,
    project_wire,
    resolve_thinking,
    wire_fingerprint,
)
from lean_weave.errors import (
    BudgetExceededError,
    ConfigError,
    InvalidCompletionError,
    ProviderError,
    UnsupportedLLMRequest,
)
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.budget import (
    effective_output_cap,
    estimate_request_tokens,
    reservation_charge,
)
from lean_weave.metrics.models import (
    AttemptJournal,
    BudgetReservation,
    ReportedUsage,
    UsageAttribution,
    normalize_usage,
    parse_usage_attribution,
)
from lean_weave.schemas.models import TokenUsage

log = get_logger("providers.deepseek")

_MAX_RETRY_AFTER_S = 30.0
_MAX_BACKOFF_S = 2.0
_BASE_BACKOFF_S = 0.25
_VALID_STAGES = {"plan", "implement", "verify", "retry", "probe"}


@dataclass(frozen=True)
class PreparedRequest:
    feature_id: str
    stage: GatewayStage
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    response_format: dict[str, Any] | None
    result_model: type[Any] | None
    thinking: ThinkingMode
    reasoning_effort: str | None
    output_cap: int
    retry_limit: int
    attribution: UsageAttribution | None = None


@dataclass(frozen=True)
class FailureClassification:
    status: str
    code: str
    retryable: bool
    retry_after: float | None = None


class DeepSeekProvider:
    """Synchronous DeepSeek chat-completion gateway.

    The provider owns an SDK client only when it constructs one. A caller may
    inject ``client`` (or an ``httpx2`` transport); injected clients are never
    closed by this provider. Call ``close()`` or use the context manager to
    release an owned client.
    """

    def __init__(
        self,
        config: LLMConfig,
        journal: AttemptJournal,
        *,
        feature_token_budget: int = 20000,
        client: OpenAI | None = None,
        transport: httpx2.BaseTransport | None = None,
        api_key: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        id_factory: Callable[[], str] | None = None,
        post_settlement_policy: Callable[[BudgetReservation], None] | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject either an SDK client or a transport, not both")
        if feature_token_budget <= 0:
            raise ValueError("feature token budget must be positive")
        self._config = config
        self._journal = journal
        self._feature_token_budget = feature_token_budget
        self._client = client
        self._transport = transport
        self._api_key = api_key
        self._clock = clock
        self._sleep = sleeper
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._post_settlement_policy = post_settlement_policy
        self._owned_client = False
        self._owned_http = False
        self._http_client: httpx2.Client | None = None

    def __enter__(self) -> "DeepSeekProvider":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned_client and self._client is not None:
            self._client.close()
            self._client = None
            self._owned_client = False
        if self._owned_http and self._http_client is not None:
            try:
                self._http_client.close()
            except Exception:  # noqa: BLE001 - best-effort release of an owned client
                pass
            self._http_client = None
            self._owned_http = False

    # -- transport setup -------------------------------------------------

    def _ensure_client(self) -> OpenAI:
        if self._client is not None:
            return self._client
        key = self._api_key if self._api_key is not None else os.getenv(self._config.api_key_env)
        if not key:
            raise ProviderError("API key is not configured")
        if key in self._config.base_url:
            raise ConfigError("Configuration field llm.base_url contains a secret value")
        # The SDK default client enables redirects. Build an owned client for
        # every construction path, not only for injected transports, so
        # redirects stay disabled (including same-origin redirects).
        try:
            http_client = httpx2.Client(
                transport=self._transport,
                timeout=self._config.timeout_s,
                follow_redirects=False,
            )
        except (TypeError, ValueError):
            raise ProviderError("Cannot initialize the provider client") from None
        self._http_client = http_client
        self._owned_http = True
        try:
            self._client = OpenAI(
                api_key=key,
                base_url=self._config.base_url,
                timeout=self._config.timeout_s,
                max_retries=0,
                http_client=http_client,
            )
        except (TypeError, ValueError):
            http_client.close()
            self._http_client = None
            self._owned_http = False
            raise ProviderError("Cannot initialize the provider client") from None
        self._owned_client = True
        log.debug("[FIX] provider client sdk_retries=0 follow_redirects=disabled")
        return self._client

    # -- request validation ----------------------------------------------

    def _prepare(self, request: LLMRequest) -> PreparedRequest:
        feature_id = request.get("feature_id")
        if not isinstance(feature_id, str) or not feature_id.strip():
            raise UnsupportedLLMRequest("A non-empty feature ID is required")
        stage = request.get("stage")
        if not isinstance(stage, str) or stage not in _VALID_STAGES:
            raise UnsupportedLLMRequest("A supported gateway stage is required")
        self._validate_attribution(feature_id, stage)

        model = request.get("model") or self._config.model
        if not isinstance(model, str) or not model.strip():
            raise UnsupportedLLMRequest("A non-empty model is required")
        capability = MODEL_CAPABILITIES.get(model)
        if capability is None:
            raise UnsupportedLLMRequest("Unsupported model")

        options = request.get("options") or {}
        if not isinstance(options, Mapping):
            raise UnsupportedLLMRequest("Gateway options must be a mapping")

        thinking = self._thinking_mode(request, options, capability)
        output_cap = self._output_cap(request, options)
        normalized = normalize_chat_request(
            model=model,
            messages=request.get("messages"),
            tools=request.get("tools"),
            response_format=request.get("response_format"),
            result_model=request.get("result_model"),
            thinking=thinking,
            reasoning_effort=request.get("reasoning_effort"),
            output_cap=output_cap,
            max_output_tokens=self._config.max_output_tokens,
        )
        retry_limit = self._retry_limit(options)
        try:
            attribution = parse_usage_attribution(request.get("usage_attribution"))
        except ValueError:
            raise UnsupportedLLMRequest("Usage attribution is invalid") from None
        if stage == PROBE_STAGE and attribution is not None:
            raise UnsupportedLLMRequest("Probe requests cannot carry attribution")

        return PreparedRequest(
            feature_id=feature_id,
            stage=stage,
            model=normalized.model,
            messages=normalized.messages,
            tools=normalized.tools,
            response_format=normalized.response_format,
            result_model=normalized.result_model,
            thinking=normalized.thinking,
            reasoning_effort=normalized.reasoning_effort,
            output_cap=normalized.output_cap,
            retry_limit=retry_limit,
            attribution=attribution,
        )

    @staticmethod
    def _validate_attribution(feature_id: str, stage: str) -> None:
        if stage == PROBE_STAGE and feature_id != PROBE_FEATURE_ID:
            raise UnsupportedLLMRequest("Probe requests require the reserved probe feature ID")
        if feature_id == PROBE_FEATURE_ID and stage != PROBE_STAGE:
            raise UnsupportedLLMRequest("The reserved probe feature ID cannot use a workflow stage")

    def _thinking_mode(
        self, request: LLMRequest, options: Mapping[str, Any], capability: Any
    ) -> ThinkingMode:
        return resolve_thinking(
            requested=request.get("thinking"),
            option=options.get("thinking"),
            configured=self._config.thinking,
            capability=capability,
        )

    def _output_cap(self, request: LLMRequest, options: Mapping[str, Any]) -> int:
        requested_values = [
            value
            for value in (request.get("max_output_tokens"), options.get("max_output_tokens"))
            if value is not None
        ]
        for value in requested_values:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise UnsupportedLLMRequest("Output token override must be a positive integer")
            if value > self._config.max_output_tokens:
                raise UnsupportedLLMRequest("Output token override exceeds the configured ceiling")
        requested = min(requested_values) if requested_values else None
        try:
            return effective_output_cap(requested, self._config.max_output_tokens)
        except ValueError:
            raise UnsupportedLLMRequest("Invalid output token limit") from None

    def _retry_limit(self, options: Mapping[str, Any]) -> int:
        selected = options.get("retry_limit")
        if selected is None:
            return self._config.max_retries
        if isinstance(selected, bool) or not isinstance(selected, int) or selected < 0:
            raise UnsupportedLLMRequest("Retry override must be a non-negative integer")
        # The option may only tighten the configured retry ceiling.
        if selected > self._config.max_retries:
            raise UnsupportedLLMRequest("Retry override exceeds the configured ceiling")
        return selected

    # -- invocation ------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        prepared = self._prepare(request)
        request_id = self._id_factory()
        wire = self._wire_request(prepared)
        self._validate_packed_guard(request, prepared, wire)
        client = self._ensure_client()
        estimated_input = estimate_request_tokens(
            messages=wire["messages"],
            tools=wire.get("tools"),
            response_format=wire.get("response_format"),
            extra=wire.get("extra_body"),
        )
        log.debug(
            "[DeepSeek.request] stage=%s feature=%s model=%s messages=%s tools=%s json=%s thinking=%s retries=%s",
            prepared.stage,
            prepared.feature_id,
            prepared.model,
            len(prepared.messages),
            len(prepared.tools or []),
            prepared.response_format is not None,
            prepared.thinking,
            prepared.retry_limit,
        )

        attempt_ordinal = 0
        while True:
            attempt_id = f"{request_id}-a{attempt_ordinal}"
            reservation = self._reserve(
                prepared=prepared,
                request_id=request_id,
                attempt_id=attempt_id,
                attempt_ordinal=attempt_ordinal,
                estimated_input=estimated_input,
            )
            log.debug(
                "[DeepSeek.attempt] attempt=%s ordinal=%s reserved=%s",
                attempt_id,
                attempt_ordinal,
                reservation.charge_tokens,
            )
            started = self._clock()
            try:
                response = client.chat.completions.create(**wire)
            except openai.OpenAIError as exc:
                duration_ms = self._duration_ms(started)
                usage = self._usage_from_error(exc)
                self._warn_incomplete_usage(attempt_id, usage)
                failure = self._classify_error(exc)
                self._journal.finalize_attempt(
                    reservation,
                    status=failure.status,
                    usage=usage,
                    model_returned=self._model_from_error(exc),
                    duration_ms=duration_ms,
                    error_code=failure.code,
                )
                self._after_settlement(reservation)
                log.warning(
                    "[DeepSeek.attempt] failed attempt=%s code=%s retryable=%s usage=%s",
                    attempt_id,
                    failure.code,
                    failure.retryable,
                    usage is not None,
                )
                if failure.retryable and attempt_ordinal < prepared.retry_limit:
                    delay = self._backoff(attempt_ordinal, failure.retry_after)
                    log.info(
                        "[DeepSeek.retry] attempt=%s delay=%.3f retries_remaining=%s",
                        attempt_id,
                        delay,
                        prepared.retry_limit - attempt_ordinal,
                    )
                    self._sleep(delay)
                    attempt_ordinal += 1
                    continue
                raise ProviderError(f"Provider request failed: {failure.code}") from None

            duration_ms = self._duration_ms(started)
            usage = self._usage_from_response(response)
            self._warn_incomplete_usage(attempt_id, usage)
            finish_reason = self._finish_reason(response)
            try:
                message, structured = self._parse_response(prepared, response)
            except InvalidCompletionError as exc:
                self._journal.finalize_attempt(
                    reservation,
                    status="invalid",
                    usage=usage,
                    model_returned=self._response_model(response),
                    duration_ms=duration_ms,
                    finish_reason=finish_reason,
                    error_code=getattr(exc, "code", "invalid_completion"),
                )
                self._after_settlement(reservation)
                log.warning(
                    "[DeepSeek.attempt] invalid attempt=%s code=%s usage=%s",
                    attempt_id,
                    getattr(exc, "code", "invalid_completion"),
                    usage is not None,
                )
                if (
                    getattr(exc, "code", "") == "blank_json"
                    and attempt_ordinal < prepared.retry_limit
                ):
                    delay = self._backoff(attempt_ordinal, None)
                    log.info(
                        "[DeepSeek.retry] attempt=%s delay=%.3f reason=blank_json",
                        attempt_id,
                        delay,
                    )
                    self._sleep(delay)
                    attempt_ordinal += 1
                    continue
                raise

            self._journal.finalize_attempt(
                reservation,
                status="success",
                usage=usage,
                model_returned=self._response_model(response),
                duration_ms=duration_ms,
                finish_reason=finish_reason,
            )
            self._after_settlement(reservation)
            log.info(
                "[DeepSeek.attempt] success attempt=%s duration_ms=%s usage=%s",
                attempt_id,
                duration_ms,
                usage is not None,
            )
            spent = getattr(self._journal, "spent", None)
            if callable(spent) and spent(prepared.feature_id) > self._feature_token_budget:
                raise BudgetExceededError("Feature token budget was exceeded after completion")
            counters = usage.counters if usage is not None else None
            return LLMResponse(
                message=message,
                usage=TokenUsage(
                    prompt_tokens=counters.input_tokens or 0 if counters else 0,
                    completion_tokens=counters.output_tokens or 0 if counters else 0,
                    total_tokens=(counters.measured_total() or 0) if counters else 0,
                    reasoning_tokens=counters.reasoning_tokens or 0 if counters else 0,
                    prompt_cache_hit_tokens=counters.cache_hit_tokens or 0 if counters else 0,
                ),
                raw_finish_reason=finish_reason,
                model=self._response_model(response),
                usage_reported=usage is not None,
                structured=structured,
            )

    def _after_settlement(self, reservation: BudgetReservation) -> None:
        """Trusted benchmark-only stop hook after the outcome is durable."""

        policy = self._post_settlement_policy
        if policy is not None:
            policy(reservation)

    def _reserve(
        self,
        *,
        prepared: PreparedRequest,
        request_id: str,
        attempt_id: str,
        attempt_ordinal: int,
        estimated_input: int,
    ) -> BudgetReservation:
        """Book exactly once, with attribution only when it is durable."""

        common = {
            "feature_id": prepared.feature_id,
            "stage": prepared.stage,
            "model_requested": prepared.model,
            "request_id": request_id,
            "attempt_id": attempt_id,
            "attempt_ordinal": attempt_ordinal,
            "estimated_input_tokens": estimated_input,
            "output_cap": prepared.output_cap,
            "budget_tokens": self._feature_token_budget,
        }
        if prepared.attribution is not None:
            attributed = getattr(self._journal, "reserve_attributed_attempt", None)
            if callable(attributed):
                return attributed(**common, attribution=prepared.attribution)
            log.debug(
                "[DeepSeek.attribution] outcome=journal_capability_missing stage=%s",
                prepared.stage,
            )
        return self._journal.reserve_attempt(**common)

    def _wire_request(self, prepared: PreparedRequest) -> dict[str, Any]:
        return project_wire(
            NormalizedChatRequest(
                model=prepared.model,
                messages=prepared.messages,
                tools=prepared.tools,
                response_format=prepared.response_format,
                result_model=prepared.result_model,
                thinking=prepared.thinking,
                reasoning_effort=prepared.reasoning_effort,
                output_cap=prepared.output_cap,
            )
        )

    def _validate_packed_guard(
        self,
        request: LLMRequest,
        prepared: PreparedRequest,
        wire: Mapping[str, Any],
    ) -> None:
        """Reject drifted packed input before client, reservation or transport."""

        if "packed_guard" not in request:
            return
        guard = request.get("packed_guard")
        if not isinstance(guard, PackedRequestGuard):
            raise UnsupportedLLMRequest("Packed request guard is malformed")
        for value in (
            guard.input_tokens,
            guard.output_tokens,
            guard.total_tokens,
            guard.output_cap,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise UnsupportedLLMRequest("Packed request guard limits are invalid")
        if guard.input_tokens + guard.output_tokens > guard.total_tokens:
            raise UnsupportedLLMRequest("Packed request guard limits are inconsistent")
        if guard.model != prepared.model or guard.output_cap != prepared.output_cap:
            raise UnsupportedLLMRequest("Packed request model or output cap changed")
        if guard.expected_fingerprint != wire_fingerprint(wire):
            raise UnsupportedLLMRequest("Packed request wire shape changed")
        log.debug(
            "[DeepSeek.packed] guard=ok fragments_total=%s model=%s cap=%s",
            guard.input_tokens,
            guard.model,
            guard.output_cap,
        )

    # -- response handling ----------------------------------------------

    def _parse_response(
        self, prepared: PreparedRequest, response: Any
    ) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
        choices = getattr(response, "choices", None)
        if not choices:
            raise InvalidCompletionError("Provider returned no completion choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        if message is None:
            raise InvalidCompletionError("Provider returned no message")
        if finish_reason == "length":
            error = InvalidCompletionError("Provider output was truncated")
            error.code = "truncated"  # type: ignore[attr-defined]
            raise error

        content = getattr(message, "content", None)
        tool_calls = self._message_tool_calls(message)
        normalized_message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            normalized_message["tool_calls"] = tool_calls
        reasoning = getattr(message, "reasoning_content", None)
        if isinstance(reasoning, str):
            normalized_message["reasoning_content"] = reasoning
        if not tool_calls and (not isinstance(content, str) or not content.strip()):
            if prepared.response_format is not None:
                error = InvalidCompletionError("Provider returned blank JSON content")
                error.code = "blank_json"  # type: ignore[attr-defined]
                raise error
            raise InvalidCompletionError("Provider returned an empty message")

        if prepared.response_format is None:
            return normalized_message, None

        try:
            decoded = json.loads(content if isinstance(content, str) else "")
        except (json.JSONDecodeError, TypeError):
            error = InvalidCompletionError("Provider JSON content is malformed")
            error.code = "malformed_json"  # type: ignore[attr-defined]
            raise error from None
        if not isinstance(decoded, dict):
            error = InvalidCompletionError("Provider JSON content is not an object")
            error.code = "non_object"  # type: ignore[attr-defined]
            raise error from None
        if prepared.result_model is not None:
            try:
                validated = prepared.result_model.model_validate(decoded)
            except ValidationError:
                error = InvalidCompletionError("Provider JSON content failed schema validation")
                error.code = "schema_invalid"  # type: ignore[attr-defined]
                raise error from None
            structured = validated.model_dump(mode="json")
        else:
            structured = decoded
        return normalized_message, structured

    @staticmethod
    def _message_tool_calls(message: Any) -> list[dict[str, Any]] | None:
        calls = getattr(message, "tool_calls", None)
        if not calls:
            return None
        normalized: list[dict[str, Any]] = []
        for call in calls:
            function = getattr(call, "function", None)
            identifier = getattr(call, "id", None)
            if not identifier or function is None:
                raise InvalidCompletionError("Provider returned a malformed tool call")
            arguments = getattr(function, "arguments", "")
            normalized.append(
                {
                    "id": identifier,
                    "type": "function",
                    "function": {
                        "name": getattr(function, "name", ""),
                        "arguments": arguments if isinstance(arguments, str) else "",
                    },
                }
            )
        return normalized

    @staticmethod
    def _finish_reason(response: Any) -> str | None:
        try:
            return response.choices[0].finish_reason
        except (AttributeError, IndexError, TypeError):
            return None

    @staticmethod
    def _response_model(response: Any) -> str | None:
        model = getattr(response, "model", None)
        if isinstance(model, str) and model:
            return model[:256]
        return None

    @staticmethod
    def _usage_from_response(response: Any) -> ReportedUsage | None:
        usage = getattr(response, "usage", None)
        return _normalize_sdk_usage(usage)

    @staticmethod
    def _usage_from_error(exc: openai.OpenAIError) -> ReportedUsage | None:
        body = getattr(exc, "body", None)
        usage = body.get("usage") if isinstance(body, Mapping) else None
        if usage is None:
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    decoded = response.json()
                except Exception:  # noqa: BLE001 - error body is best effort only
                    decoded = None
                if isinstance(decoded, Mapping):
                    usage = decoded.get("usage")
        return _normalize_sdk_usage(usage)

    @staticmethod
    def _model_from_error(exc: openai.OpenAIError) -> str | None:
        body = getattr(exc, "body", None)
        if isinstance(body, Mapping):
            model = body.get("model")
            if isinstance(model, str) and model:
                return model[:256]
        return None

    @staticmethod
    def _classify_error(exc: openai.OpenAIError) -> FailureClassification:
        if isinstance(exc, openai.APITimeoutError):
            return FailureClassification("error", "timeout", True)
        if isinstance(exc, openai.APIConnectionError):
            return FailureClassification("error", "connection", True)
        status_code = getattr(exc, "status_code", None)
        retry_after = _retry_after(getattr(exc, "response", None))
        if status_code == 429:
            return FailureClassification("error", "rate_limited", True, retry_after)
        if status_code == 408 or (isinstance(status_code, int) and status_code >= 500):
            return FailureClassification("error", "upstream_unavailable", True, retry_after)
        if isinstance(exc, openai.APIResponseValidationError):
            return FailureClassification("invalid", "invalid_response", False)
        if isinstance(status_code, int) and 400 <= status_code < 500:
            return FailureClassification("error", "rejected_request", False)
        return FailureClassification("error", "provider_error", False)

    def _duration_ms(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))

    @staticmethod
    def _warn_incomplete_usage(attempt_id: str, usage: ReportedUsage | None) -> None:
        if usage is not None and not usage.counters.core_complete():
            log.warning(
                "[DeepSeek.attempt] incomplete_usage attempt=%s measured=false",
                attempt_id,
            )

    @staticmethod
    def _backoff(attempt_ordinal: int, retry_after: float | None) -> float:
        delay = min(_MAX_BACKOFF_S, _BASE_BACKOFF_S * (2**attempt_ordinal))
        if retry_after is not None:
            delay = max(delay, min(_MAX_RETRY_AFTER_S, retry_after))
        return delay


def _normalize_sdk_usage(usage: Any) -> ReportedUsage | None:
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        raw: Any = dict(usage)
    elif hasattr(usage, "model_dump"):
        try:
            raw = usage.model_dump(exclude_none=False)
            extras = getattr(usage, "model_extra", None)
            if isinstance(extras, Mapping):
                raw.update(extras)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return normalize_usage(raw)


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return delay
