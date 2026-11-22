from __future__ import annotations

import json
from pathlib import Path

from lean_weave.config.models import ContextConfig, PackingConfig
from lean_weave.context.packer import ContextPacker
from lean_weave.context.packing_models import ExpansionRequest, SymbolSelector
from lean_weave.context.packing_models import PackRequest
from lean_weave.context.packing_source import slice_lines
from lean_weave.context.repository import Repository
from lean_weave.context.retrieval_models import RetrievalResult
from lean_weave.context.retriever import ContextRetriever


def packing(**overrides) -> PackingConfig:
    base = {
        "total_tokens": 8000,
        "output_tokens": 500,
        "system_tokens": 1000,
        "task_tokens": 1000,
        "map_tokens": 800,
        "source_tokens": 3000,
        "tool_tokens": 2000,
        "max_expansions": 2,
        "cumulative_source_tokens": 8000,
    }
    base.update(overrides)
    return PackingConfig(**base)


def make_packer(project: Path, *, config: PackingConfig | None = None, counter=None, load_graph=None):
    context = ContextConfig(packing=config or packing())
    repository = Repository(project, context, strict_source=True)
    retriever = ContextRetriever(repository, load_graph=load_graph)
    return ContextPacker(
        repository, retriever=retriever, token_counter=counter
    )


def request(**overrides) -> PackRequest:
    payload = {
        "task_id": "task-1",
        "query": "helper",
        "task_specification": "Change the helper safely.",
        "system_instructions": "Use only supplied source.",
        "level": "l1",
    }
    payload.update(overrides)
    return PackRequest(**payload)


def test_l1_pack_has_complete_definition_and_provenance(basic_repo: Path):
    packer = make_packer(basic_repo)
    result = packer.build(request())
    assert result.status == "ready"
    pack = result.pack
    assert pack is not None
    assert pack.messages
    assert pack.fragments
    fragment = pack.fragments[0]
    snapshot = packer.repository.read(fragment.path)
    assert fragment.text == slice_lines(
        snapshot.text, fragment.start_line, fragment.end_line
    )
    assert fragment.digest == snapshot.digest
    assert pack.source_revisions
    assert fragment.fragment_id in pack.included_fragment_ids
    assert pack.metrics.output_tokens == 500
    assert pack.metrics.input_tokens + 500 == pack.metrics.total_tokens


def test_l0_is_map_only(basic_repo: Path):
    packer = make_packer(basic_repo)
    result = packer.build(request(level="l0"))
    assert result.status == "ready"
    pack = result.pack
    assert pack is not None
    assert pack.fragments == ()
    assert pack.map_rows
    user = pack.messages[-1]["content"]
    assert "<source_fragments>" not in user
    assert "<repository_map>" in user


def test_missing_required_symbol_is_explicit(basic_repo: Path):
    packer = make_packer(basic_repo)
    result = packer.build(
        request(
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.absent"),
            )
        )
    )
    assert result.status == "insufficient_context"
    assert "required_source_unavailable" in result.reason_codes
    assert result.pack is not None and result.pack.messages == ()


def test_no_direct_hit_is_explicit(basic_repo: Path):
    packer = make_packer(basic_repo)
    result = packer.build(request(query="zzzz-unmatchable"))
    assert result.status == "insufficient_context"
    assert "no_relevant_source" in result.reason_codes


def test_required_large_function_is_not_clipped(project: Path):
    body = "\n".join(f"    line_{index} = {index}" for index in range(400))
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("")
    (project / "pkg" / "huge.py").write_text(
        "def huge_target():\n" + body + "\n"
    )
    packer = make_packer(project, config=packing(source_tokens=80))
    result = packer.build(request(query="huge_target"))
    assert result.status == "insufficient_context"
    assert "required_source_over_budget" in result.reason_codes
    assert result.pack is not None and result.pack.fragments == ()


def test_optional_giant_body_is_omitted_not_trimmed(project: Path):
    body = "\n".join(f"    payload_{index} = 'small'" for index in range(300))
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("")
    (project / "pkg" / "mixed.py").write_text(
        "def small():\n    return 1\n\n\ndef giant():\n" + body + "\n"
    )
    packer = make_packer(project, config=packing(source_tokens=200))
    result = packer.build(request(query="small"))
    assert result.status == "ready"
    pack = result.pack
    assert pack is not None
    assert [item.symbol for item in pack.fragments] == ["small"]
    assert "budget" in pack.metrics.omitted_by_reason


def test_zero_map_budget_still_builds_l1(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(map_tokens=0))
    result = packer.build(request())
    assert result.status == "ready"
    assert result.pack is not None
    assert result.pack.map_rows == ()
    assert result.pack.fragments


def test_l0_with_zero_map_budget_is_insufficient(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(map_tokens=0))
    result = packer.build(request(level="l0"))
    assert result.status == "insufficient_context"
    assert "map_unavailable" in result.reason_codes


def test_required_class_covers_method_without_duplicate(basic_repo: Path):
    packer = make_packer(basic_repo)
    result = packer.build(
        request(
            required_symbols=(
                SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.Widget"),
                SymbolSelector(
                    path="pkg/core.py", qualified_name="pkg.core.Widget.render"
                ),
            )
        )
    )
    assert result.status == "ready"
    pack = result.pack
    assert pack is not None
    widget_fragments = [item for item in pack.fragments if item.symbol == "Widget"]
    assert len(widget_fragments) == 1
    assert "pkg.core.Widget.render" in widget_fragments[0].covered_selectors
    assert all(item.symbol != "Widget.render" for item in pack.fragments)


def test_repeated_builds_are_deterministic(basic_repo: Path):
    packer = make_packer(basic_repo)
    first = packer.build(request())
    second = packer.build(request())
    assert first.pack is not None and second.pack is not None
    assert first.pack.included_fragment_ids == second.pack.included_fragment_ids
    assert first.pack.messages == second.pack.messages
    assert first.pack.metrics == second.pack.metrics


def test_unicode_source_bytes_are_preserved(project: Path):
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("")
    (project / "pkg" / "unicode_mod.py").write_text(
        "def grüß():\n    return 'héllo λ'\n"
    )
    packer = make_packer(project)
    result = packer.build(request(query="grüß"))
    assert result.status == "ready"
    assert result.pack is not None
    assert "héllo λ" in result.pack.fragments[0].text
    assert "héllo λ" in result.pack.messages[-1]["content"]


def test_source_changed_before_publish_is_stale(basic_repo: Path):
    packer = make_packer(basic_repo)

    def changed(request, *, allowed_paths=None, on_ranked=None):
        return (
            RetrievalResult(
                hits=(),
                task_kind=request.task_kind,
                kind=request.kind,
                top_k=10,
                degraded_reasons=("source_changed",),
                truncated=True,
            ),
            None,
        )

    packer.retriever.rank_checked = changed  # type: ignore[method-assign]
    result = packer.build(request())
    assert result.status == "stale_reference"
    assert "source_changed" in result.reason_codes


def test_source_unavailable_is_typed(basic_repo: Path):
    packer = make_packer(basic_repo)

    def unavailable(request, *, allowed_paths=None, on_ranked=None):
        return (
            RetrievalResult(
                hits=(),
                task_kind=request.task_kind,
                kind=request.kind,
                top_k=10,
                degraded_reasons=("source_unavailable",),
            ),
            None,
        )

    packer.retriever.rank_checked = unavailable  # type: ignore[method-assign]
    result = packer.build(request())
    assert result.status == "insufficient_context"
    assert "source_unavailable" in result.reason_codes


def test_exact_source_budget_fits_and_one_under_fails(basic_repo: Path):
    exact_request = request(
        query="zzzz-unmatchable",
        required_symbols=(
            SymbolSelector(path="pkg/core.py", qualified_name="pkg.core.helper"),
        ),
    )
    probe = make_packer(basic_repo, config=packing(source_tokens=3000))
    first = probe.build(exact_request)
    assert first.status == "ready" and first.pack is not None
    assert len(first.pack.fragments) == 1
    exact = first.pack.metrics.section_tokens["source"]
    exact_packer = make_packer(
        basic_repo, config=packing(source_tokens=exact)
    )
    assert exact_packer.build(exact_request).status == "ready"
    short_packer = make_packer(
        basic_repo, config=packing(source_tokens=exact - 1)
    )
    short = short_packer.build(exact_request)
    assert short.status == "insufficient_context"
    assert "required_source_over_budget" in short.reason_codes


class _ExplodingCounter:
    name = "local_tokenizer"

    def supports(self, **_):
        return True

    def count_text(self, text: str) -> int:
        raise RuntimeError("adapter exploded")

    def count_request(self, payload) -> int:
        raise RuntimeError("adapter exploded")


def test_counter_failure_restarts_once_with_conservative_estimate(basic_repo: Path):
    packer = make_packer(basic_repo, counter=_ExplodingCounter())
    result = packer.build(request())
    assert result.status == "ready"
    assert result.pack is not None
    assert result.pack.metrics.estimator == "conservative_estimate"


def test_cumulative_budget_exact_fit_and_one_over(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(max_expansions=4))
    opened, probe = packer.open_task(request())
    assert opened.status == "ready" and probe is not None
    before = probe.cumulative_units
    digest = probe.revisions["pkg/core.py"]
    expanded = packer.expand(
        probe,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert expanded.status == "ready"
    delta = probe.cumulative_units - before
    cap = packer.packing_config.cumulative_source_tokens

    exact_opened, exact_session = packer.open_task(request())
    assert exact_session is not None
    exact_session.cumulative_units = cap - delta
    exact = packer.expand(
        exact_session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert exact.status == "ready"

    over_opened, over_session = packer.open_task(request())
    assert over_session is not None
    over_session.cumulative_units = cap - delta + 1
    over = packer.expand(
        over_session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert over.status == "insufficient_context"
    assert "cumulative_budget" in over.reason_codes


class _LocalCounter:
    name = "local_tokenizer"

    def supports(self, **_):
        return True

    def count_text(self, text: str) -> int:
        return len(text)

    def count_request(self, payload) -> int:
        return len(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def test_explicit_local_counter_is_never_reported_as_usage(basic_repo: Path):
    packer = make_packer(basic_repo, counter=_LocalCounter())
    result = packer.build(request())
    assert result.status == "ready"
    assert result.pack is not None
    assert result.pack.metrics.estimator == "local_tokenizer"
