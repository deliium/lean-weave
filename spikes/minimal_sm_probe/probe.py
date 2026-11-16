"""Deterministic state machine modeling plan→implement→verify→retry."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from lean_weave_spike_logging import configure_spike_logging

log = configure_spike_logging()


class Stage(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    RETRY = "retry"
    DONE = "done"
    FAILED = "failed"


@dataclass
class FeatureStateLite:
    feature_id: str
    stage: Stage = Stage.PLAN
    plan_summary: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    current_task_index: int = 0
    verify_ok: bool | None = None
    retry_count: int = 0
    max_retries: int = 2
    interrupt: dict[str, Any] | None = None
    metrics: dict[str, int] = field(default_factory=lambda: {"token_calls": 0})

    def to_json(self) -> str:
        payload = asdict(self)
        payload["stage"] = self.stage.value
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, text: str) -> FeatureStateLite:
        data = json.loads(text)
        data["stage"] = Stage(data["stage"])
        return cls(**data)


class MinimalStateMachine:
    """Explicit transition table; interrupt = persisted FeatureState + flag."""

    TRANSITIONS = {
        (Stage.PLAN, "planned"): Stage.IMPLEMENT,
        (Stage.IMPLEMENT, "task_done"): Stage.IMPLEMENT,
        (Stage.IMPLEMENT, "all_tasks_done"): Stage.VERIFY,
        (Stage.IMPLEMENT, "pause"): Stage.IMPLEMENT,  # stays; interrupt set
        (Stage.VERIFY, "pass"): Stage.DONE,
        (Stage.VERIFY, "fail"): Stage.RETRY,
        (Stage.RETRY, "requeue"): Stage.IMPLEMENT,
        (Stage.RETRY, "exhausted"): Stage.FAILED,
    }

    def __init__(self, state: FeatureStateLite, store_path: Path | None = None) -> None:
        self.state = state
        self.store_path = store_path

    def _persist(self) -> None:
        if self.store_path is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(self.state.to_json(), encoding="utf-8")
        log.debug(
            "[OrchestratorCompare.evaluate] persisted state {option=minimal_sm, score=n/a} path=%s stage=%s",
            self.store_path,
            self.state.stage.value,
        )

    def _transition(self, event: str) -> None:
        key = (self.state.stage, event)
        if key not in self.TRANSITIONS:
            raise RuntimeError(f"invalid transition {key}")
        nxt = self.TRANSITIONS[key]
        log.debug(
            "[OrchestratorCompare.evaluate] transition {option=minimal_sm, score=n/a} %s --%s--> %s",
            self.state.stage.value,
            event,
            nxt.value,
        )
        self.state.stage = nxt
        self._persist()

    def plan(self, summary: str, tasks: list[str]) -> None:
        assert self.state.stage is Stage.PLAN
        self.state.plan_summary = summary
        self.state.tasks = [{"id": i, "title": t, "status": "pending"} for i, t in enumerate(tasks)]
        self.state.metrics["token_calls"] += 1  # simulated LLM plan call
        self._transition("planned")

    def pause(self, reason: str) -> None:
        self.state.interrupt = {"reason": reason, "task_index": self.state.current_task_index}
        self._transition("pause")
        self._persist()

    def resume(self) -> None:
        if self.state.interrupt is None:
            raise RuntimeError("no interrupt to resume")
        self.state.interrupt = None
        self._persist()

    def implement_next(self, succeed: bool = True) -> None:
        if self.state.interrupt is not None:
            raise RuntimeError("paused; call resume() first")
        assert self.state.stage is Stage.IMPLEMENT
        if self.state.current_task_index >= len(self.state.tasks):
            self._transition("all_tasks_done")
            return
        task = self.state.tasks[self.state.current_task_index]
        self.state.metrics["token_calls"] += 1
        task["status"] = "done" if succeed else "failed"
        self.state.current_task_index += 1
        if self.state.current_task_index >= len(self.state.tasks):
            self._transition("all_tasks_done")
        else:
            self._transition("task_done")

    def verify(self, ok: bool) -> None:
        assert self.state.stage is Stage.VERIFY
        self.state.verify_ok = ok
        self._transition("pass" if ok else "fail")

    def retry(self) -> None:
        assert self.state.stage is Stage.RETRY
        self.state.retry_count += 1
        if self.state.retry_count > self.state.max_retries:
            self._transition("exhausted")
            return
        # Requeue last failed / all tasks for simplicity: reset pending from current-1
        self.state.current_task_index = max(0, self.state.current_task_index - 1)
        for t in self.state.tasks[self.state.current_task_index :]:
            t["status"] = "pending"
        self.state.verify_ok = None
        self._transition("requeue")


def run_happy_path(store: Path | None = None) -> FeatureStateLite:
    t0 = time.perf_counter()
    sm = MinimalStateMachine(FeatureStateLite(feature_id="feat-happy"), store)
    sm.plan("demo", ["t1", "t2"])
    sm.implement_next()
    sm.pause("human review")
    # durable resume
    if store is not None:
        sm2 = MinimalStateMachine(FeatureStateLite.from_json(store.read_text()), store)
        sm2.resume()
        sm2.implement_next()
        sm2.verify(True)
        state = sm2.state
    else:
        sm.resume()
        sm.implement_next()
        sm.verify(True)
        state = sm.state
    elapsed = time.perf_counter() - t0
    log.info(
        "[OrchestratorCompare.evaluate] happy path {option=minimal_sm, score=1} elapsed_ms=%.2f stage=%s",
        elapsed * 1000,
        state.stage.value,
    )
    return state


def run_retry_path() -> FeatureStateLite:
    sm = MinimalStateMachine(FeatureStateLite(feature_id="feat-retry"))
    sm.plan("demo", ["t1"])
    sm.implement_next()
    sm.verify(False)
    sm.retry()
    sm.implement_next()
    sm.verify(True)
    log.info(
        "[OrchestratorCompare.evaluate] retry path {option=minimal_sm, score=1} retries=%s stage=%s",
        sm.state.retry_count,
        sm.state.stage.value,
    )
    return sm.state


if __name__ == "__main__":
    print(run_happy_path().to_json())
    print(run_retry_path().to_json())
