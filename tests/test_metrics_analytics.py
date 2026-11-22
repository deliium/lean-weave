from __future__ import annotations

import json

import httpx2
import pytest

from lean_weave.config import AppConfig
from lean_weave.metrics.analytics import aggregate_usage
from lean_weave.metrics.jsonl import JsonlMetricsCollector
from lean_weave.metrics.models import UsageAttribution, normalize_usage
from lean_weave.providers.deepseek import DeepSeekProvider


def usage(prompt=10, completion=6, total=16, **extra):
    return normalize_usage(
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            **extra,
        }
    )


def reserve(collector, *, feature="f1", stage="plan", ordinal=0, attribution=None):
    return collector.reserve_attributed_attempt(
        feature_id=feature,
        stage=stage,
        model_requested="deepseek-flash",
        request_id=f"req-{ordinal}",
        attempt_id=f"req-{ordinal}-a{ordinal}",
        attempt_ordinal=ordinal,
        estimated_input_tokens=10,
        output_cap=20,
        budget_tokens=1000,
        attribution=attribution
        or UsageAttribution(task_id="1", strategy="v2", stage_attempt=1),
    )


def test_attribution_survives_outcome_and_interrupted_reservation(tmp_path):
    collector = JsonlMetricsCollector(tmp_path / "state")
    first = reserve(collector)
    collector.finalize_attempt(
        first,
        status="success",
        usage=normalize_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 6,
                "total_tokens": 16,
                "prompt_cache_hit_tokens": 4,
                "completion_tokens_details": {"reasoning_tokens": 2},
            }
        ),
        model_returned="deepseek-flash",
        duration_ms=7,
    )
    reserve(collector, ordinal=1)
    snapshot = collector.reporting_snapshot("f1")
    assert snapshot.available is True
    assert len(snapshot.attempts) == 2
    assert snapshot.attempts[0].task_id == "1"
    assert snapshot.attempts[0].strategy == "v2"
    assert snapshot.attempts[0].stage_attempt == 1
    assert snapshot.attempts[1].status == "unresolved"
    report = aggregate_usage(snapshot)
    assert report.totals is not None
    assert report.totals.total_tokens == 16
    assert report.totals.cache_hit_tokens == 4
    assert report.totals.reasoning_tokens == 2
    assert report.unresolved_charge > 0
    assert report.budget_charge == report.spent


def test_grouped_conservation_and_unknown_buckets(tmp_path):
    collector = JsonlMetricsCollector(tmp_path / "state")
    complete = reserve(collector, attribution=UsageAttribution(task_id="1", strategy="v1", stage_attempt=1))
    collector.finalize_attempt(complete, status="success", usage=usage(), model_returned="deepseek-flash")
    partial = reserve(
        collector,
        ordinal=1,
        attribution=UsageAttribution(task_id="2", strategy="v1_fallback", stage_attempt=2),
    )
    collector.finalize_attempt(
        partial,
        status="error",
        usage=normalize_usage({"prompt_tokens": 4, "completion_tokens": 0}),
        error_code="timeout",
    )
    snapshot = collector.reporting_snapshot("f1")
    by_task = aggregate_usage(snapshot, group_by=("task",))
    assert {row.dimensions["task"] for row in by_task.rows} == {"1", "2"}
    assert by_task.totals is not None
    assert by_task.totals.total_tokens == 20
    assert by_task.totals.failed_attempts == 1
    assert by_task.totals.coverage.complete()["total_tokens"] is False


def test_reported_contradiction_is_flagged_not_repaired(tmp_path):
    collector = JsonlMetricsCollector(tmp_path / "state")
    item = reserve(collector)
    collector.finalize_attempt(item, status="success", usage=usage(prompt=10, completion=6, total=99))
    report = aggregate_usage(collector.reporting_snapshot("f1"))
    assert report.totals is not None
    assert report.totals.total_tokens == 99
    assert report.totals.contradictory_totals == 1


def _provider(tmp_path, handler):
    config = AppConfig.model_validate(
        {"llm": {"max_retries": 1, "max_output_tokens": 32}, "workflow": {"feature_token_budget": 10_000}}
    )
    journal = JsonlMetricsCollector(tmp_path / "state")
    provider = DeepSeekProvider(
        config.llm,
        journal,
        feature_token_budget=10_000,
        transport=httpx2.MockTransport(handler),
        api_key="sk-test",
        sleeper=lambda _: None,
    )
    return provider, journal


def test_attributed_provider_retry_books_once_per_transport_attempt(tmp_path):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx2.Response(
                429,
                json={
                    "error": {"message": "slow"},
                    "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        )

    provider, journal = _provider(tmp_path, handler)
    provider.complete(
        {
            "feature_id": "f1",
            "stage": "plan",
            "messages": [{"role": "user", "content": "hello"}],
            "usage_attribution": {"task_id": "1", "strategy": "v2", "stage_attempt": 1},
        }
    )
    assert "usage_attribution" not in calls[0]
    snapshot = journal.reporting_snapshot("f1")
    assert len(snapshot.attempts) == 2
    assert all(item.task_id == "1" for item in snapshot.attempts)
    assert all(item.strategy == "v2" for item in snapshot.attempts)
    assert snapshot.attempts[0].status == "error"
    assert snapshot.attempts[1].status == "success"


def test_old_journal_double_gets_one_booking(tmp_path):
    class OldJournal:
        def __init__(self):
            self.calls = 0

        def reserve_attempt(self, **kwargs):
            self.calls += 1
            raise RuntimeError("booked")

    config = AppConfig.model_validate({"llm": {"max_retries": 0}})
    journal = OldJournal()
    provider = DeepSeekProvider(config.llm, journal, feature_token_budget=1000, api_key="sk-test")
    with pytest.raises(RuntimeError):
        provider.complete(
            {
                "feature_id": "f1",
                "stage": "plan",
                "messages": [{"role": "user", "content": "hello"}],
                "usage_attribution": {"task_id": "1", "strategy": "v1", "stage_attempt": 1},
            }
        )
    assert journal.calls == 1


def test_invalid_attribution_is_rejected_before_booking(tmp_path):
    provider, journal = _provider(tmp_path, lambda request: httpx2.Response(200, json={}))
    with pytest.raises(Exception):
        provider.complete(
            {
                "feature_id": "f1",
                "stage": "plan",
                "messages": [{"role": "user", "content": "hello"}],
                "usage_attribution": {"task_id": "", "strategy": "v1", "stage_attempt": 0},
            }
        )
    assert journal.reporting_snapshot("f1").available is False
