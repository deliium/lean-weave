from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from deepseek_probe.fixtures import FIXTURE_JSON
from lean_weave.config.models import AppConfig
from lean_weave.context.packing_gateway import prepare_packed_request
from lean_weave.context.packing_models import ExpansionRequest, PackEnvelope, SymbolSelector
from lean_weave.errors import (
    BudgetExceededError,
    InvalidContextRequest,
    UnsupportedLLMRequest,
)
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.providers.deepseek import DeepSeekProvider

from tests.context.packing_support import make_packer, packing, request


@pytest.fixture
def basic_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("")
    (tmp_path / "pkg/core.py").write_text(
        "import json\n\n"
        "class Widget:\n"
        "    def render(self, value: str = 'x') -> str:\n"
        "        return json.dumps({'value': value})\n\n"
        "def helper(a, /, b, *args, c=1, **kwargs):\n"
        "    return a + b + c\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_core.py").write_text(
        "from pkg.core import helper\n\ndef test_helper():\n    assert helper(1, 2) == 4\n"
    )
    return tmp_path


def payload(fixture, **overrides):
    data = json.loads(json.dumps(fixture))
    data["created"] = 0
    data.update(overrides)
    return data


def make_provider(tmp_path: Path, handler, *, budget: int = 100_000):
    config = AppConfig.model_validate(
        {
            "llm": {"max_output_tokens": 1024},
            "workflow": {"feature_token_budget": budget},
        }
    )
    journal = JsonlMetricsCollector(tmp_path / "state")
    provider = DeepSeekProvider(
        config.llm,
        journal,
        feature_token_budget=budget,
        transport=httpx2.MockTransport(handler),
        api_key="sk-test",
        sleeper=lambda _: None,
    )
    return provider, journal


def ready_pack(project: Path):
    packer = make_packer(project)
    result = packer.build(request())
    assert result.status == "ready" and result.pack is not None
    return packer, result.pack


def test_ready_pack_dispatches_exact_body(tmp_path: Path, basic_repo: Path):
    packer, pack = ready_pack(basic_repo)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _journal = make_provider(tmp_path, handler)
    prepared = prepare_packed_request(
        pack,
        feature_id="f1",
        stage="implement",
        outbound_paths=[fragment.path for fragment in pack.fragments]
        + [row.path for row in pack.map_rows],
    )
    response = provider.complete(prepared)
    assert response["usage"].total_tokens == 42
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["messages"] == list(pack.messages)
    assert body["max_tokens"] == pack.metrics.output_tokens
    assert "packed_guard" not in body
    assert "source_revisions" not in json.dumps(body)


def test_tampered_message_is_rejected_before_transport(
    tmp_path: Path, basic_repo: Path
):
    _packer, pack = ready_pack(basic_repo)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _ = make_provider(tmp_path, handler)
    prepared = prepare_packed_request(
        pack,
        feature_id="f1",
        stage="implement",
        outbound_paths=[fragment.path for fragment in pack.fragments]
        + [row.path for row in pack.map_rows],
    )
    prepared["messages"][0]["content"] = "tampered"
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(prepared)
    assert sent == []


def test_explicit_none_guard_is_rejected(tmp_path: Path, basic_repo: Path):
    _packer, pack = ready_pack(basic_repo)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _ = make_provider(tmp_path, handler)
    prepared = prepare_packed_request(
        pack,
        feature_id="f1",
        stage="implement",
        outbound_paths=[fragment.path for fragment in pack.fragments]
        + [row.path for row in pack.map_rows],
    )
    prepared["packed_guard"] = None
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(prepared)
    assert sent == []


def test_non_ready_pack_and_scope_are_refused(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(source_tokens=1))
    failed = packer.build(request())
    assert failed.pack is not None
    with pytest.raises(InvalidContextRequest):
        prepare_packed_request(
            failed.pack, feature_id="f1", stage="implement", outbound_paths=[]
        )

    _packer, pack = ready_pack(basic_repo)
    with pytest.raises(InvalidContextRequest):
        prepare_packed_request(
            pack, feature_id="f1", stage="implement", outbound_paths=[]
        )


def test_guard_rejection_does_not_touch_feature_ledger(
    tmp_path: Path, basic_repo: Path
):
    _packer, pack = ready_pack(basic_repo)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, journal = make_provider(tmp_path, handler)
    prepared = prepare_packed_request(
        pack,
        feature_id="f1",
        stage="implement",
        outbound_paths=[fragment.path for fragment in pack.fragments]
        + [row.path for row in pack.map_rows],
    )
    prepared["messages"][0]["content"] = "tampered"
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(prepared)
    assert sent == []
    assert journal.spent("f1") == 0


def test_independent_feature_budget_refuses_before_transport(
    tmp_path: Path, basic_repo: Path
):
    _packer, pack = ready_pack(basic_repo)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _ = make_provider(tmp_path, handler, budget=1)
    prepared = prepare_packed_request(
        pack,
        feature_id="f1",
        stage="implement",
        outbound_paths=[fragment.path for fragment in pack.fragments]
        + [row.path for row in pack.map_rows],
    )
    with pytest.raises(BudgetExceededError):
        provider.complete(prepared)
    assert sent == []


def test_l2_delivery_and_replay_all_comparison(tmp_path: Path, basic_repo: Path):
    packer = make_packer(basic_repo)
    opened, session = packer.open_task(request(query="helper"))
    assert opened.status == "ready" and session is not None
    digest = session.revisions["pkg/core.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert expanded.status == "ready"
    receipt = session.receipts[expanded.receipt_id]
    delivery = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, receipt.carry_forward_ids
    )
    assert delivery.status == "ready" and delivery.pack is not None
    cumulative = session.cumulative_units
    delivery_again = packer.prepare_expansion_delivery(
        session, expanded.receipt_id, receipt.carry_forward_ids
    )
    assert delivery_again.status == "ready"
    assert session.cumulative_units == cumulative

    replay_all = packer.build(
        request(
            query="helper",
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.helper"),
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.Widget"),
            ),
        )
    )
    assert replay_all.pack is not None
    assert delivery.pack.metrics.input_tokens < replay_all.pack.metrics.input_tokens


def test_expansion_delivery_rejects_wrong_carry_ids(basic_repo: Path):
    packer = make_packer(basic_repo)
    opened, session = packer.open_task(request(query="helper"))
    assert session is not None
    digest = session.revisions["pkg/core.py"]
    expanded = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert expanded.receipt_id is not None
    with pytest.raises(InvalidContextRequest):
        packer.prepare_expansion_delivery(
            session, expanded.receipt_id, ["frag:sha256:foreign"]
        )


def test_packed_thinking_replay_is_counted_and_guarded(
    tmp_path: Path, basic_repo: Path
):
    packer = make_packer(basic_repo)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    replay = (
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "look first",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
    )
    result = packer.build(
        request(
            envelope=PackEnvelope(
                model="deepseek-flash",
                thinking="enabled",
                tools=tools,
                replay=replay,
            )
        )
    )
    assert result.status == "ready" and result.pack is not None
    assert result.pack.tools
    assert result.pack.metrics.section_tokens["tool"] > 0
    sent = []

    def handler(request_object):
        sent.append(request_object)
        return httpx2.Response(200, json=payload(FIXTURE_JSON))

    provider, _ = make_provider(tmp_path, handler)
    prepared = prepare_packed_request(
        result.pack,
        feature_id="f1",
        stage="implement",
        outbound_paths=[fragment.path for fragment in result.pack.fragments]
        + [row.path for row in result.pack.map_rows],
    )
    provider.complete(prepared)
    body = json.loads(sent[0].content)
    assert body["tools"] == tools
    assert body["messages"][1]["reasoning_content"] == "look first"
    assert body["messages"][2]["tool_call_id"] == "call-1"

    prepared["messages"][1]["reasoning_content"] = "tampered"
    with pytest.raises(UnsupportedLLMRequest):
        provider.complete(prepared)
    assert len(sent) == 1
