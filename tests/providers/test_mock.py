import pytest

from lean_weave.errors import ProviderError
from lean_weave.providers.mock import MockLLMProvider
from lean_weave.schemas import TokenUsage


def response(content="ok", total=3):
    return {
        "message": {"role": "assistant", "content": content},
        "usage": TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=total),
        "usage_reported": True,
    }


def test_script_order_capture_and_exhaustion():
    provider = MockLLMProvider([response("one"), ValueError("typed failure")])
    first = provider.complete({"feature_id": "f", "stage": "plan", "messages": []})
    assert first["synthetic"] is True
    assert first["message"]["content"] == "one"
    assert provider.requests[0]["feature_id"] == "f"
    with pytest.raises(ValueError, match="typed failure"):
        provider.complete({"feature_id": "f", "stage": "plan", "messages": []})
    with pytest.raises(ProviderError, match="exhausted"):
        provider.complete({"feature_id": "f", "stage": "plan", "messages": []})
    assert len(provider.requests) == 3
    assert provider.remaining == 0


def test_response_is_copied_to_avoid_mutation():
    scripted = [response("ok")]
    provider = MockLLMProvider(scripted)
    result = provider.complete({})
    result["message"]["content"] = "mutated"
    assert scripted[0]["message"]["content"] == "ok"
