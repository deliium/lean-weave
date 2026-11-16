from __future__ import annotations

from typing import Protocol

from ._types import LLMRequest, LLMResponse


class LLMProvider(Protocol):
    """DeepSeek (OpenAI-compatible) client boundary.

    Must support thinking on/off, tools, reasoning_content echo when tools
    present, response_format json_object, and usage/reasoning/cache fields.
    Sync for MVP 1. Forbidden: MCP clients, multi-provider routers.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Perform one chat completion; never log API keys."""
        ...
