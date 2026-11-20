"""Stage policies and transition helpers outside the driver.

The transition table stays the schema-owned ``TRANSITIONS`` baseline. Explicit
terminal/error paths are separate, named operations so illegal edges cannot be
invented by string concatenation.
"""

from __future__ import annotations

from collections.abc import Sequence

from lean_weave.errors import InvalidTransition
from lean_weave.logging_setup import get_logger
from lean_weave.schemas import FeaturePlan, FeatureState, ImplementationTask, Stage, TaskStatus
from lean_weave.schemas.models import TRANSITIONS
from lean_weave.workflow.results import WorkflowStatus

log = get_logger("workflow.stages")


def transition(state: FeatureState, event: str) -> Stage:
    key = (state.stage, event)
    target = TRANSITIONS.get(key)
    if target is None:
        log.warning(
            "[Workflow.transition] from=%s event=%s outcome=illegal",
            state.stage.value,
            event,
        )
        raise InvalidTransition(f"Illegal transition from {state.stage.value} via {event}")
    previous = state.stage
    state.stage = target
    log.debug(
        "[Workflow.transition] %s --%s--> %s",
        previous.value,
        event,
        target.value,
    )
    return target


def terminal(state: FeatureState, status: WorkflowStatus, reason: str) -> None:
    state.stage = Stage.DONE if status is WorkflowStatus.SUCCESS else Stage.FAILED
    log.info(
        "[Workflow.terminal] stage=%s status=%s reason=%s",
        state.stage.value,
        status.value,
        reason,
    )


def ready_task(plan: FeaturePlan, active_task_id: str | None) -> ImplementationTask | None:
    """Return the resumable active task or the first dependency-ready task."""

    if active_task_id:
        for task in plan.tasks:
            if task.id == active_task_id and task.status in {
                TaskStatus.PENDING,
                TaskStatus.IN_PROGRESS,
            }:
                return task
    for task in plan.tasks:
        if task.status is not TaskStatus.PENDING:
            continue
        dependencies = {item.id: item.status for item in plan.tasks}
        if all(dependencies.get(dep) is TaskStatus.DONE for dep in task.depends_on):
            return task
    return None


def pending_dependency_failure(plan: FeaturePlan) -> bool:
    """True when remaining tasks depend on a task that did not complete."""

    by_id = {task.id: task for task in plan.tasks}
    for task in plan.tasks:
        if task.status is not TaskStatus.PENDING:
            continue
        for dependency in task.depends_on:
            state = by_id.get(dependency)
            if state is not None and state.status in {TaskStatus.FAILED, TaskStatus.SKIPPED}:
                return True
    return False


def tasks_complete(plan: FeaturePlan) -> bool:
    return all(task.status is TaskStatus.DONE for task in plan.tasks)


def eligible_task_paths(tasks: Sequence[ImplementationTask]) -> set[str]:
    return {path for task in tasks for path in task.files}
