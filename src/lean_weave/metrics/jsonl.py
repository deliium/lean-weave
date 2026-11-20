"""Append-only JSONL usage journal with atomic budget admission."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lean_weave.config.loader import resolve_state_dir
from lean_weave.contracts._types import PROBE_FEATURE_ID, PROBE_STAGE, GatewayStage, StageName
from lean_weave.errors import BudgetExceededError, JournalError
from lean_weave.logging_setup import get_logger
from lean_weave.metrics.budget import charge_for_usage, reservation_charge
from lean_weave.metrics.models import (
    AttemptSummary,
    BudgetReservation,
    FeatureUsageSummary,
    JournalEvent,
    ModelUsageSummary,
    ReportedUsage,
    StageUsageSummary,
    UsageCounters,
    UsagePresence,
    normalize_usage,
    token_usage_from_counters,
)
from lean_weave.schemas.models import TokenUsage

log = get_logger("metrics.jsonl")

JOURNAL_RELATIVE_PATH = Path("metrics") / "llm-usage.jsonl"
JOURNAL_NAME = "llm-usage.jsonl"
_LOCK_WAIT_S = 2.0
_LOCK_RETRY_S = 0.01

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

_TERMINAL_STATUSES = {"success", "error", "invalid", "budget"}
_VALID_STAGES = {"plan", "implement", "verify", "retry", "probe"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _canonical(event: JournalEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _add_counter(current: int | None, value: int | None) -> int | None:
    if value is None:
        return current
    return value if current is None else current + value


def _accumulate_counters(target: UsageCounters, source: UsageCounters) -> None:
    target.input_tokens = _add_counter(target.input_tokens, source.input_tokens)
    target.output_tokens = _add_counter(target.output_tokens, source.output_tokens)
    target.total_tokens = _add_counter(target.total_tokens, source.total_tokens)
    target.cache_hit_tokens = _add_counter(target.cache_hit_tokens, source.cache_hit_tokens)
    target.reasoning_tokens = _add_counter(target.reasoning_tokens, source.reasoning_tokens)


class JsonlMetricsCollector:
    """Single-writer-per-journal accounting implementation.

    Reservations are fsynced before transport. Admission replays the journal,
    checks cumulative feature spend, and appends the reservation while holding
    one process-shared lock, so separately constructed collectors for the same
    canonical file cannot both bypass the budget.
    """

    def __init__(self, state_dir: Path | str, *, journal_name: str = JOURNAL_NAME) -> None:
        if (
            not journal_name
            or journal_name in {".", ".."}
            or Path(journal_name).name != journal_name
        ):
            raise JournalError("Usage journal name must be a plain file name")
        root = Path(state_dir)
        try:
            self._state_dir = root.resolve()
        except (OSError, RuntimeError, ValueError):
            raise JournalError("Cannot resolve usage journal directory") from None
        self._path = self._state_dir / "metrics" / journal_name
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        if not self._path.resolve(strict=False).is_relative_to(self._state_dir):
            raise JournalError("Usage journal path escapes the state directory")

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        journal_name: str = JOURNAL_NAME,
        root: Path | None = None,
    ) -> "JsonlMetricsCollector":
        if root is not None:
            state_dir = (Path(root) / config.workflow.state_dir).resolve()
            return cls(state_dir, journal_name=journal_name)
        return cls(resolve_state_dir(config), journal_name=journal_name)

    @property
    def journal_path(self) -> Path:
        return self._path

    # -- locking ---------------------------------------------------------

    def _ensure_layout(self) -> None:
        metrics_dir = self._path.parent
        for parent in (self._state_dir, metrics_dir):
            if parent.is_symlink():
                raise JournalError("Usage journal directory must not be a symlink")
            if parent.exists() and not parent.is_dir():
                raise JournalError("Usage journal directory is not a directory")
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise JournalError("Cannot create usage journal directory") from None

        for candidate in (self._path, self._lock_path):
            if candidate.is_symlink():
                raise JournalError("Usage journal or lock must not be a symlink")
            if candidate.exists() and not _regular_file(candidate):
                raise JournalError("Usage journal or lock must be a regular file")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with _thread_lock(self._path):
            self._ensure_layout()
            try:
                fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
            except OSError:
                raise JournalError("Cannot open usage journal lock") from None
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise JournalError("Usage journal lock must be a regular file")
                deadline = time.monotonic() + _LOCK_WAIT_S
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise JournalError("Usage journal is locked by another writer") from None
                        time.sleep(_LOCK_RETRY_S)
                    except OSError:
                        raise JournalError("Usage journal locking is unavailable") from None
                try:
                    yield
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -- replay ----------------------------------------------------------

    def _read_events_locked(self) -> list[JournalEvent]:
        try:
            return self._read_events_locked_impl()
        except JournalError as exc:
            log.error("[Metrics.replay] outcome=failed code=%s", exc)
            raise

    def _read_events_locked_impl(self) -> list[JournalEvent]:
        if self._path.is_symlink():
            raise JournalError("Usage journal must not be a symlink")
        if not self._path.exists():
            return []
        if not _regular_file(self._path):
            raise JournalError("Usage journal must be a regular file")
        events: list[JournalEvent] = []
        try:
            with self._path.open("r", encoding="utf-8", errors="strict") as stream:
                for number, line in enumerate(stream, start=1):
                    if not line.endswith("\n"):
                        raise JournalError(f"Usage journal has a partial trailing line ({number})")
                    text = line.strip()
                    if not text:
                        raise JournalError(f"Usage journal contains an empty line ({number})")
                    try:
                        events.append(JournalEvent.model_validate(json.loads(text)))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        raise JournalError(f"Usage journal contains a malformed line ({number})") from None
        except (OSError, UnicodeError):
            raise JournalError("Cannot read usage journal") from None

        # Reject unsupported versions, conflicting duplicates, and orphan or
        # duplicate terminal events. Exact duplicate events are idempotent.
        deduped: list[JournalEvent] = []
        by_id: dict[str, JournalEvent] = {}
        for event in events:
            if event.version != 1:
                raise JournalError("Unsupported usage journal version")
            previous = by_id.get(event.event_id)
            if previous is not None:
                if _canonical(previous) == _canonical(event):
                    continue
                raise JournalError("Conflicting duplicate usage journal event")
            by_id[event.event_id] = event
            deduped.append(event)

        reservations: set[str] = set()
        reservation_events: dict[str, JournalEvent] = {}
        ended: set[str] = set()
        for event in deduped:
            if event.event == "reservation":
                if event.reservation_id is None:
                    raise JournalError("Usage journal reservation has no identifier")
                if event.reservation_id in reservations:
                    raise JournalError("Usage journal has duplicate reservations")
                reservations.add(event.reservation_id)
                reservation_events[event.reservation_id] = event
        for event in deduped:
            if event.event != "outcome":
                continue
            if event.reservation_id is None or event.reservation_id not in reservations:
                raise JournalError("Usage journal outcome has no reservation")
            if event.reservation_id in ended:
                raise JournalError("Usage journal has duplicate terminal outcomes")
            reservation = reservation_events[event.reservation_id]
            if (
                event.feature_id != reservation.feature_id
                or event.stage != reservation.stage
                or event.model_requested != reservation.model_requested
                or event.request_id != reservation.request_id
                or event.attempt_id != reservation.attempt_id
                or event.attempt_ordinal != reservation.attempt_ordinal
            ):
                raise JournalError("Usage journal outcome attribution conflicts with reservation")
            ended.add(event.reservation_id)
        return deduped

    def _append_locked(self, event: JournalEvent) -> JournalEvent:
        try:
            return self._append_locked_impl(event)
        except JournalError as exc:
            log.error("[Metrics.append] event=%s outcome=failed code=%s", event.event, exc)
            raise

    def _append_locked_impl(self, event: JournalEvent) -> JournalEvent:
        existing = self._read_events_locked()
        by_id: dict[str, JournalEvent] = {item.event_id: item for item in existing}
        previous = by_id.get(event.event_id)
        if previous is not None:
            if _canonical(previous) == _canonical(event):
                return previous
            if self._semantically_equal(previous, event):
                return previous
            raise JournalError("Conflicting duplicate usage journal event")

        if event.event == "outcome":
            reservations = {
                item.reservation_id
                for item in existing
                if item.event == "reservation" and item.reservation_id is not None
            }
            if event.reservation_id not in reservations:
                raise JournalError("Usage journal outcome has no reservation")
            outcomes = {
                item.reservation_id
                for item in existing
                if item.event == "outcome" and item.reservation_id is not None
            }
            if event.reservation_id in outcomes:
                raise JournalError("Conflicting duplicate terminal usage outcome")

        line = _canonical(event) + "\n"
        created = not self._path.exists()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError:
            raise JournalError("Cannot open usage journal for append") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise JournalError("Usage journal must be a regular file")
            encoded = line.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                written = os.write(fd, encoded[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(fd)
        except OSError:
            raise JournalError("Cannot append usage journal event") from None
        finally:
            os.close(fd)
        if created:
            self._sync_parent()
        return event

    @staticmethod
    def _semantically_equal(left: JournalEvent, right: JournalEvent) -> bool:
        if left.event != right.event or left.event_id != right.event_id:
            return False
        left_data = left.model_dump(exclude={"created_at"})
        right_data = right.model_dump(exclude={"created_at"})
        return left_data == right_data

    def _sync_parent(self) -> None:
        try:
            fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except (OSError, AttributeError):
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # -- admission -------------------------------------------------------

    def reserve_attempt(
        self,
        *,
        feature_id: str,
        stage: GatewayStage,
        model_requested: str,
        request_id: str,
        attempt_id: str,
        attempt_ordinal: int,
        estimated_input_tokens: int,
        output_cap: int,
        budget_tokens: int,
    ) -> BudgetReservation:
        self._validate_attribution(feature_id, stage)
        if budget_tokens <= 0:
            raise JournalError("Feature token budget must be positive")
        if attempt_ordinal < 0 or estimated_input_tokens < 0 or output_cap < 0:
            raise JournalError("Attempt accounting values must not be negative")
        if not request_id or not attempt_id or not model_requested:
            raise JournalError("Attempt accounting identifiers are required")
        charge = reservation_charge(estimated_input_tokens, output_cap)
        with self._locked():
            events = self._read_events_locked()
            if self._spent_from_events(events, feature_id) + charge > budget_tokens:
                log.info(
                    "[Metrics.budget] exhausted feature=%s requested=%s budget=%s",
                    feature_id,
                    charge,
                    budget_tokens,
                )
                raise BudgetExceededError("Feature token budget exhausted")
            event_id = f"{attempt_id}:reservation"
            event = JournalEvent(
                event="reservation",
                event_id=event_id,
                reservation_id=attempt_id,
                request_id=request_id,
                attempt_id=attempt_id,
                attempt_ordinal=attempt_ordinal,
                feature_id=feature_id,
                stage=stage,
                model_requested=model_requested,
                created_at=_now(),
                estimated_input_tokens=estimated_input_tokens,
                output_cap=output_cap,
                charge_tokens=charge,
                reservation_charge=charge,
                status="reserved",
            )
            stored = self._append_locked(event)
        log.debug(
            "[Metrics.reserve] feature=%s attempt=%s ordinal=%s charge=%s",
            feature_id,
            attempt_id,
            attempt_ordinal,
            charge,
        )
        return BudgetReservation(
            reservation_id=attempt_id,
            request_id=stored.request_id,
            attempt_id=stored.attempt_id,
            attempt_ordinal=stored.attempt_ordinal,
            feature_id=stored.feature_id,
            stage=stored.stage,  # type: ignore[arg-type]
            model_requested=stored.model_requested,
            created_at=stored.created_at,
            estimated_input_tokens=stored.estimated_input_tokens or 0,
            output_cap=stored.output_cap or 0,
            charge_tokens=stored.charge_tokens or 0,
        )

    def finalize_attempt(
        self,
        reservation: BudgetReservation,
        *,
        status: str,
        usage: ReportedUsage | None = None,
        model_returned: str | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        error_code: str | None = None,
    ) -> JournalEvent:
        if status not in _TERMINAL_STATUSES:
            raise JournalError("Unsupported terminal usage status")
        if duration_ms is not None and duration_ms < 0:
            raise JournalError("Attempt duration must not be negative")
        charge, _measured = charge_for_usage(usage, reserved=reservation.charge_tokens)
        with self._locked():
            events = self._read_events_locked()
            found = None
            for item in events:
                if item.event == "reservation" and item.reservation_id == reservation.reservation_id:
                    found = item
                    break
            if found is None:
                raise JournalError("Usage journal reservation is missing")
            if (
                found.feature_id != reservation.feature_id
                or found.stage != reservation.stage
                or found.model_requested != reservation.model_requested
            ):
                raise JournalError("Usage journal reservation does not match attribution")
            for item in events:
                if item.event == "outcome" and item.reservation_id == reservation.reservation_id:
                    if (
                        item.status == status
                        and item.charge_tokens == charge
                        and item.model_returned == model_returned
                        and item.error_code == error_code
                        and item.usage == usage
                    ):
                        return item
                    raise JournalError("Conflicting duplicate terminal usage outcome")

            event = JournalEvent(
                event="outcome",
                event_id=f"{reservation.reservation_id}:outcome",
                reservation_id=reservation.reservation_id,
                request_id=reservation.request_id,
                attempt_id=reservation.attempt_id,
                attempt_ordinal=reservation.attempt_ordinal,
                feature_id=reservation.feature_id,
                stage=reservation.stage,
                model_requested=reservation.model_requested,
                model_returned=model_returned,
                created_at=_now(),
                charge_tokens=charge,
                reservation_charge=reservation.charge_tokens,
                usage=usage,
                status=status,
                error_code=error_code,
                duration_ms=duration_ms,
                finish_reason=finish_reason,
            )
            stored = self._append_locked(event)
        log.debug(
            "[Metrics.outcome] feature=%s attempt=%s status=%s charge=%s retry=%s",
            reservation.feature_id,
            reservation.attempt_id,
            status,
            charge,
            reservation.attempt_ordinal > 0,
        )
        return stored

    def _validate_attribution(self, feature_id: str, stage: str) -> None:
        if not isinstance(feature_id, str) or not feature_id.strip():
            raise JournalError("Feature ID must be non-empty")
        if stage not in _VALID_STAGES:
            raise JournalError("Unsupported gateway stage")
        if stage == PROBE_STAGE and feature_id != PROBE_FEATURE_ID:
            raise JournalError("Probe accounting requires the reserved probe feature ID")
        if feature_id == PROBE_FEATURE_ID and stage != PROBE_STAGE:
            raise JournalError("The reserved probe feature ID cannot use a workflow stage")

    # -- compatibility and queries --------------------------------------

    def record_usage(self, feature_id: str, stage: StageName, usage: TokenUsage) -> None:
        """Book one standalone legacy usage record exactly once."""
        self._validate_attribution(feature_id, stage)
        normalized = normalize_usage(
            {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "prompt_cache_hit_tokens": usage.prompt_cache_hit_tokens,
                "completion_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
            }
        )
        charge, _ = charge_for_usage(
            normalized,
            reserved=max(
                usage.total_tokens,
                usage.prompt_tokens + usage.completion_tokens,
            ),
        )
        identifier = f"legacy-{uuid.uuid4().hex}"
        event = JournalEvent(
            event="legacy_usage",
            event_id=f"{identifier}:usage",
            request_id=identifier,
            attempt_id=identifier,
            feature_id=feature_id,
            stage=stage,
            model_requested="legacy",
            created_at=_now(),
            charge_tokens=charge,
            usage=normalized,
            status="success",
            legacy=True,
        )
        with self._locked():
            self._append_locked(event)
        log.debug("[Metrics.legacy] feature=%s charge=%s", feature_id, charge)

    def totals(self, feature_id: str) -> TokenUsage:
        return self.summary(feature_id).measured

    def _journal_absent(self) -> bool:
        # A dangling symlink reports exists() == False through Path.exists().
        # Treat it as present so replay rejects it instead of resetting spend.
        return not self._path.exists() and not self._path.is_symlink()

    def spent(self, feature_id: str) -> int:
        if self._journal_absent():
            return 0
        with self._locked():
            return self._spent_from_events(self._read_events_locked(), feature_id)

    def summary(self, feature_id: str) -> FeatureUsageSummary:
        if self._journal_absent():
            return FeatureUsageSummary(feature_id=feature_id)
        with self._locked():
            events = self._read_events_locked()
        return self._summary_from_events(events, feature_id)

    # -- aggregation -----------------------------------------------------

    @staticmethod
    def _spent_from_events(events: list[JournalEvent], feature_id: str) -> int:
        reservations = {
            item.reservation_id: item
            for item in events
            if item.event == "reservation" and item.feature_id == feature_id
        }
        outcomes = {
            item.reservation_id: item
            for item in events
            if item.event == "outcome" and item.feature_id == feature_id
        }
        spend = 0
        for reservation_id, reservation in reservations.items():
            outcome = outcomes.get(reservation_id)
            reserved = reservation.charge_tokens or 0
            if outcome is None:
                spend += reserved
            else:
                spend += charge_for_usage(outcome.usage, reserved=reserved)[0]
        for item in events:
            if item.event == "legacy_usage" and item.feature_id == feature_id:
                spend += item.charge_tokens or 0
        return spend

    def _summary_from_events(
        self, events: list[JournalEvent], feature_id: str
    ) -> FeatureUsageSummary:
        summary = FeatureUsageSummary(feature_id=feature_id)
        stage_map: dict[str, StageUsageSummary] = {}
        stage_model_map: dict[tuple[str, str], ModelUsageSummary] = {}
        model_map: dict[str, ModelUsageSummary] = {}
        reservations: dict[str, JournalEvent] = {}
        outcomes: dict[str, JournalEvent] = {}

        for item in events:
            if item.event == "reservation" and item.feature_id == feature_id:
                reservations[item.reservation_id or item.attempt_id] = item
            elif item.event == "outcome" and item.feature_id == feature_id:
                outcomes[item.reservation_id or item.attempt_id] = item

        def stage_for(stage: str) -> StageUsageSummary:
            if stage not in stage_map:
                stage_map[stage] = StageUsageSummary(stage=stage)
            return stage_map[stage]

        def model_for(stage: str, model: str) -> ModelUsageSummary:
            key = (stage, model)
            if key not in stage_model_map:
                stage_model_map[key] = ModelUsageSummary(model_requested=model)
            return stage_model_map[key]

        def global_model_for(model: str) -> ModelUsageSummary:
            if model not in model_map:
                model_map[model] = ModelUsageSummary(model_requested=model)
            return model_map[model]

        for reservation_id, reservation in reservations.items():
            outcome = outcomes.get(reservation_id)
            reserved = reservation.charge_tokens or 0
            stage = stage_for(reservation.stage)
            model = model_for(reservation.stage, reservation.model_requested)
            global_model = global_model_for(reservation.model_requested)
            stage.attempts += 1
            summary.attempts_total += 1
            model.attempts += 1
            global_model.attempts += 1
            if reservation.attempt_ordinal > 0:
                stage.retry_sends += 1
                summary.retry_sends += 1

            if outcome is None:
                summary.unresolved_attempts += 1
                summary.unresolved_charge += reserved
                summary.budget_charge += reserved
                stage.estimated_charge += reserved
                model.estimated_charge += reserved
                global_model.estimated_charge += reserved
                summary.attempts.append(
                    AttemptSummary(
                        reservation_id=reservation_id,
                        request_id=reservation.request_id,
                        attempt_id=reservation.attempt_id,
                        attempt_ordinal=reservation.attempt_ordinal,
                        is_retry=reservation.attempt_ordinal > 0,
                        stage=reservation.stage,
                        model_requested=reservation.model_requested,
                        status="unresolved",
                        charge_tokens=reserved,
                        estimated_charge=reserved,
                    )
                )
                continue

            usage = outcome.usage
            counters = usage.counters if usage is not None else UsageCounters()
            charge, measured = charge_for_usage(usage, reserved=reserved)
            status = outcome.status or "error"
            summary.budget_charge += charge
            if measured:
                summary.measured_charge += charge
                stage.measured_charge += charge
                model.measured_charge += charge
                global_model.measured_charge += charge
            else:
                summary.estimated_charge += charge
                stage.estimated_charge += charge
                model.estimated_charge += charge
                global_model.estimated_charge += charge
            if status != "success":
                stage.failed_attempts += 1
                summary.failed_attempts += 1
            if outcome.duration_ms is not None:
                stage.duration_ms += outcome.duration_ms
            if usage is not None:
                _accumulate_counters(summary.counters, counters)
                summary.presence = summary.presence.merge(counters)
                _accumulate_counters(stage.measured, counters)
                stage.presence = stage.presence.merge(counters)
                _accumulate_counters(model.measured, counters)
                model.presence = model.presence.merge(counters)
                _accumulate_counters(global_model.measured, counters)
                global_model.presence = global_model.presence.merge(counters)
                if outcome.model_returned:
                    model.model_returned = outcome.model_returned
                    global_model.model_returned = outcome.model_returned
            summary.attempts.append(
                AttemptSummary(
                    reservation_id=reservation_id,
                    request_id=reservation.request_id,
                    attempt_id=reservation.attempt_id,
                    attempt_ordinal=reservation.attempt_ordinal,
                    is_retry=reservation.attempt_ordinal > 0,
                    stage=reservation.stage,
                    model_requested=reservation.model_requested,
                    model_returned=outcome.model_returned,
                    status=status,
                    error_code=outcome.error_code,
                    finish_reason=outcome.finish_reason,
                    duration_ms=outcome.duration_ms,
                    charge_tokens=charge,
                    measured_charge=charge if measured else 0,
                    estimated_charge=0 if measured else charge,
                    measured=measured,
                    counters=counters,
                )
            )

        for item in events:
            if item.event != "legacy_usage" or item.feature_id != feature_id:
                continue
            usage = item.usage
            counters = usage.counters if usage is not None else UsageCounters()
            charge = item.charge_tokens or 0
            measured = usage is not None and usage.counters.core_complete()
            stage = stage_for(item.stage)
            model = model_for(item.stage, item.model_requested)
            global_model = global_model_for(item.model_requested)
            stage.attempts += 1
            summary.attempts_total += 1
            model.attempts += 1
            global_model.attempts += 1
            summary.budget_charge += charge
            if measured:
                summary.measured_charge += charge
                stage.measured_charge += charge
                model.measured_charge += charge
                global_model.measured_charge += charge
            else:
                summary.estimated_charge += charge
                stage.estimated_charge += charge
                model.estimated_charge += charge
                global_model.estimated_charge += charge
            if usage is not None:
                _accumulate_counters(summary.counters, counters)
                summary.presence = summary.presence.merge(counters)
                _accumulate_counters(stage.measured, counters)
                stage.presence = stage.presence.merge(counters)
                _accumulate_counters(model.measured, counters)
                model.presence = model.presence.merge(counters)
                _accumulate_counters(global_model.measured, counters)
                global_model.presence = global_model.presence.merge(counters)
            summary.attempts.append(
                AttemptSummary(
                    request_id=item.request_id,
                    attempt_id=item.attempt_id,
                    attempt_ordinal=0,
                    is_retry=False,
                    stage=item.stage,
                    model_requested=item.model_requested,
                    status="legacy",
                    charge_tokens=charge,
                    measured_charge=charge if measured else 0,
                    estimated_charge=0 if measured else charge,
                    measured=measured,
                    counters=counters,
                )
            )

        summary.measured = token_usage_from_counters(summary.counters)
        for stage in stage_map.values():
            stage.models = [
                model
                for (stage_name, _model), model in stage_model_map.items()
                if stage_name == stage.stage
            ]
        summary.stages = list(stage_map.values())
        summary.models = list(model_map.values())
        return summary
