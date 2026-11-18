"""Deterministic scripted provider for offline tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from lean_weave.contracts._types import LLMRequest, LLMResponse
from lean_weave.errors import ProviderError
from lean_weave.logging_setup import get_logger

log = get_logger("providers.mock")


class MockLLMProvider:
    """Replay a scripted sequence of responses or typed failures.

    All values are synthetic test data, never measured DeepSeek usage.
    """

    def __init__(self, script: Sequence[Mapping[str, Any] | Exception]) -> None:
        self._script = list(script)
        self.requests: list[LLMRequest] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(deepcopy(request))
        log.debug(
            "[MockLLM.complete] call=%s model=%s remaining=%s",
            len(self.requests),
            request.get("model") or "mock",
            self.remaining,
        )
        if not self._script:
            raise ProviderError("Mock provider script exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        response: dict[str, Any] = deepcopy(dict(step))
        response["synthetic"] = True
        response.setdefault("model", "mock")
        return response  # type: ignore[return-value]
