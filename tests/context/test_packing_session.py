from __future__ import annotations

from pathlib import Path

import pytest

from lean_weave.context.packing_models import ExpansionRequest, FragmentRef
from lean_weave.context.packing_session import PackingSession
from lean_weave.context.repository import SourceScan
from lean_weave.errors import InvalidContextRequest

from .packing_support import make_packer, packing, request


def digest_for(session: PackingSession, path: str = "pkg/core.py") -> str:
    return session.revisions[path]


def test_open_task_returns_session_only_when_ready(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert result.status == "ready"
    assert session is not None
    assert session.open
    assert session.cumulative_units > 0
    assert all(isinstance(item, FragmentRef) for item in session.fragments.values())
    assert all(not hasattr(item, "text") for item in session.fragments.values())


def test_open_task_insufficient_has_no_session(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(source_tokens=1))
    result, session = packer.open_task(request())
    assert result.status == "insufficient_context"
    assert session is None


def test_attempt_limit_includes_successful_attempts(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(max_expansions=1))
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    first = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    assert first.status in {"ready", "no_change"}
    assert session.attempts == 1
    second = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    assert second.status == "insufficient_context"
    assert "expansion_limit" in second.reason_codes


def test_attempt_limit_includes_failed_structural_attempt(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(max_expansions=1))
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    failed = packer.expand(
        session,
        ExpansionRequest(
            kind="range",
            path="pkg/core.py",
            expected_digest=digest,
            start_line=2,
            end_line=5,
        ),
    )
    assert failed.status == "insufficient_context"
    assert session.attempts == 1
    blocked = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    assert "expansion_limit" in blocked.reason_codes


def test_zero_attempts_blocks_before_scan(basic_repo: Path):
    packer = make_packer(basic_repo, config=packing(max_expansions=0))
    result, session = packer.open_task(request())
    assert session is not None
    blocked = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest_for(session),
            qualified_name="pkg.core.helper",
        ),
    )
    assert blocked.status == "insufficient_context"
    assert "expansion_limit" in blocked.reason_codes
    assert session.attempts == 0


def test_closed_session_is_rejected(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    session.close()
    with pytest.raises(InvalidContextRequest):
        packer.expand(
            session,
            ExpansionRequest(
                kind="symbol",
                path="pkg/core.py",
                expected_digest="sha256:x",
                qualified_name="pkg.core.helper",
            ),
        )


def test_caller_digest_typo_does_not_consume_attempt(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    with pytest.raises(InvalidContextRequest):
        packer.expand(
            session,
            ExpansionRequest(
                kind="symbol",
                path="pkg/core.py",
                expected_digest="sha256:typo",
                qualified_name="pkg.core.helper",
            ),
        )
    assert session.attempts == 0
    assert session.open


def test_changed_source_invalidates_session(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    (basic_repo / "pkg/core.py").write_text(
        "def helper():\n    return 'changed'\n"
    )
    stale = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert stale.status == "stale_reference"
    assert not session.open


def test_unrelated_unrecorded_edit_does_not_invalidate(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    (basic_repo / "README.md").write_text("unrelated\n")
    ready = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    assert ready.status in {"ready", "no_change"}


def test_repeated_covered_selector_returns_no_change(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    first = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    if first.status == "ready":
        again = packer.expand(
            session,
            ExpansionRequest(
                kind="symbol",
                path="pkg/core.py",
                expected_digest=digest,
                qualified_name="pkg.core.helper",
            ),
        )
        assert again.status == "no_change"
        assert again.pack is not None and again.pack.messages == ()


def test_ignore_policy_admission_change_is_stale(basic_repo: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    (basic_repo / ".gitignore").write_text("pkg/core.py\n")
    stale = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    assert stale.status == "stale_reference"


def test_session_from_another_checkout_is_stale(basic_repo: Path, tmp_path: Path):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    other = tmp_path / "other"
    (other / "pkg").mkdir(parents=True)
    (other / "pkg/core.py").write_text("def helper():\n    return 1\n")
    other_packer = make_packer(other)
    stale = other_packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest_for(session),
            qualified_name="pkg.core.helper",
        ),
    )
    assert stale.status == "stale_reference"


def test_mutation_during_final_revalidation_is_stale(
    monkeypatch: pytest.MonkeyPatch, basic_repo: Path
):
    packer = make_packer(basic_repo)
    result, session = packer.open_task(request())
    assert session is not None
    digest = digest_for(session)
    monkeypatch.setattr(SourceScan, "revalidate", lambda self: False)
    stale = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.Widget",
        ),
    )
    assert stale.status == "stale_reference"
    assert not session.open


def test_incomplete_scan_without_recorded_witness_is_insufficient(
    basic_repo: Path,
):
    packer = make_packer(basic_repo, visited_entries_max=10)
    result, session = packer.open_task(request())
    assert result.status == "ready" and session is not None
    digest = digest_for(session)
    (basic_repo / ".gitignore").write_text("pkg/\n")
    for index in range(30):
        path = basic_repo / "aaa" / f"f{index:02d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def f{index}():\n    return {index}\n")
    blocked = packer.expand(
        session,
        ExpansionRequest(
            kind="symbol",
            path="pkg/core.py",
            expected_digest=digest,
            qualified_name="pkg.core.helper",
        ),
    )
    assert blocked.status == "insufficient_context"
    assert "source_scan_incomplete" in blocked.reason_codes
    assert session.open
