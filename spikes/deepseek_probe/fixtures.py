"""Offline fixture response shapes aligned with DeepSeek docs (2026)."""

from __future__ import annotations

FIXTURE_TOOL_CALL = {
    "id": "chatcmpl-offline-tool",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning_content": None,
                "tool_calls": [
                    {
                        "id": "call_offline_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"text":"hello"}',
                        },
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 40,
        "completion_tokens": 18,
        "total_tokens": 58,
        "prompt_tokens_details": {"cached_tokens": 0},
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 40,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}

FIXTURE_THINKING_TOOLS = {
    "id": "chatcmpl-offline-think-tools",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Need to call echo with text=hi before answering.",
                "tool_calls": [
                    {
                        "id": "call_offline_2",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"text":"hi"}',
                        },
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 55,
        "completion_tokens": 72,
        "total_tokens": 127,
        "prompt_tokens_details": {"cached_tokens": 0},
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 55,
        "completion_tokens_details": {"reasoning_tokens": 48},
    },
}

FIXTURE_JSON = {
    "id": "chatcmpl-offline-json",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": '{"ok": true, "stage": "plan"}',
                "reasoning_content": None,
            },
        }
    ],
    "usage": {
        "prompt_tokens": 30,
        "completion_tokens": 12,
        "total_tokens": 42,
        "prompt_tokens_details": {"cached_tokens": 0},
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}

# Matrix note: thinking ON without tools — reasoning_content present; must NOT
# require echo on subsequent turns (API ignores it).
FIXTURE_THINKING_NO_TOOLS = {
    "id": "chatcmpl-offline-think",
    "object": "chat.completion",
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "4",
                "reasoning_content": "2+2=4",
            },
        }
    ],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 20,
        "total_tokens": 32,
        "prompt_tokens_details": {"cached_tokens": 0},
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 12,
        "completion_tokens_details": {"reasoning_tokens": 15},
    },
}
