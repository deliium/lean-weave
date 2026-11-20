"""Deterministic ``VerificationRunner`` over one approved check registry.

The runner owns orchestration policy, not execution policy: ``run_checks``
still enforces the approval, batch deadline, and isolation boundary. Failures
are mapped to compact ``VerificationResult`` values. Only recognized diagnostic
codes with validated task-relative locations can authorize a model repair.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from lean_weave.executor.approval import ToolApproval
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import FeatureState, VerificationResult, VerificationStatus
from lean_weave.verify.checks import run_checks
from lean_weave.verify.process import IsolationBackend

log = get_logger("verify.runner")

REPAIRABLE_CODES = frozenset({"E999", "F401", "F841", "E501", "PY001"})
NON_REPAIRABLE_CODES = frozenset({"TIMEOUT", "OUTPUT_LIMIT", "SPAWN", "UNRECOGNIZED"})
MAX_REPAIR_BLOCKERS = 8


@dataclass(frozen=True)
class RepairAttribution:
    paths: tuple[str, ...]
    blockers: tuple[dict[str, object], ...]


def preflight_checks(
    approval: ToolApproval,
    check_ids: Sequence[str],
    *,
    backend: IsolationBackend | None = None,
) -> str | None:
    """Return a refusal code, or None when every required check can run."""

    if not check_ids:
        return "no_required_checks"
    if not approval.checks_approved:
        return "no_checks"
    registry = {check.id: check for check in approval.checks}
    if len(check_ids) != len(set(check_ids)):
        return "duplicate_check"
    if any(identifier not in registry for identifier in check_ids):
        return "unknown_check"
    if len(check_ids) > approval.limits.checks_max:
        return "invalid"
    if backend is None and any(
        registry[identifier].runs_workspace_code for identifier in check_ids
    ):
        return "isolation_unavailable"
    return None


class ApprovedChecksRunner:
    """Map fixed approved check outcomes to ``VerificationResult``."""

    def __init__(
        self,
        approval: ToolApproval,
        *,
        check_ids: Sequence[str],
        backend: IsolationBackend | None = None,
    ) -> None:
        self.approval = approval
        self.check_ids = tuple(check_ids)
        self.backend = backend

    def preflight(self) -> str | None:
        code = preflight_checks(self.approval, self.check_ids, backend=self.backend)
        if code is not None:
            log.warning("[Verification.preflight] outcome=refused code=%s", code)
        return code

    def run(self, feature_state: FeatureState) -> VerificationResult:
        del feature_state
        code = self.preflight()
        if code is not None:
            return self._refusal(code)
        result = run_checks(
            self.approval,
            {"check_ids": list(self.check_ids)},
            backend=self.backend,
        )
        if result.status in {
            "blocked",
            "no_checks",
            "unknown_check",
            "duplicate_check",
            "invalid",
        }:
            log.warning("[Verification.run] outcome=refused code=%s", result.status)
            return self._refusal(result.status)

        blockers: list[dict[str, object]] = []
        affected: set[str] = set()
        summaries: list[str] = []
        for item in result.items:
            summaries.append(f"{item.check_id}:{item.status}")
            if item.status == "passed":
                continue
            if item.status in {"timed_out", "output_limited", "not_run"}:
                blockers.append({"check_id": item.check_id, "code": item.status.upper()})
                continue
            if not item.diagnostics:
                blockers.append({"check_id": item.check_id, "code": "check_failed"})
                continue
            for diagnostic in item.diagnostics:
                blocker: dict[str, object] = {
                    "check_id": item.check_id,
                    "code": diagnostic.code,
                }
                if diagnostic.path is not None:
                    blocker["path"] = diagnostic.path
                    affected.add(diagnostic.path)
                if diagnostic.line is not None:
                    blocker["line"] = diagnostic.line
                blockers.append(blocker)

        if not blockers:
            log.info("[Verification.run] outcome=pass gates=%s", len(summaries))
            return VerificationResult(
                status=VerificationStatus.PASS,
                blocking=False,
                gate_summaries=summaries,
            )

        attribution = repair_attribution(
            blockers, set(self._eligible_paths()), affected
        )
        log.info(
            "[Verification.run] outcome=fail blockers=%s repairable=%s",
            len(blockers),
            attribution is not None,
        )
        return VerificationResult(
            status=VerificationStatus.FAIL,
            blocking=True,
            blockers=blockers,
            affected_files=sorted(affected),
            gate_summaries=summaries,
            suggested_next="retry" if attribution is not None else None,
        )

    def _eligible_paths(self) -> set[str]:
        return set(self.approval.write_allow)

    @staticmethod
    def _refusal(code: str) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.FAIL,
            blocking=True,
            blockers=[{"code": code}],
            gate_summaries=[],
        )


def repair_attribution(
    blockers: Iterable[dict[str, object]],
    eligible_paths: set[str],
    affected: Iterable[str] = (),
) -> RepairAttribution | None:
    """Return a unique, repairable attribution or None for ambiguity/infrastructure."""

    chosen: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    paths: set[str] = set()
    for blocker in blockers:
        code = blocker.get("code")
        path = blocker.get("path")
        line = blocker.get("line")
        if not isinstance(code, str) or code in NON_REPAIRABLE_CODES:
            return None
        if code not in REPAIRABLE_CODES:
            return None
        if not isinstance(path, str) or path not in eligible_paths:
            return None
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            return None
        key = (code, path, line)
        if key in seen:
            continue
        seen.add(key)
        paths.add(path)
        chosen.append({"code": code, "path": path, "line": line})
        if len(chosen) > MAX_REPAIR_BLOCKERS:
            return None
    if not chosen:
        return None
    if not paths or not paths.issubset(set(affected)):
        return None
    return RepairAttribution(paths=tuple(sorted(paths)), blockers=tuple(chosen))
