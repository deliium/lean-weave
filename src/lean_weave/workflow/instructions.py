"""Bounded, stage-keyed project instructions.

Fragments are short, project-owned excerpts distilled from the AI Factory
methodology references. They are data: a caller may select only registered
fragment ids for the current stage. Arbitrary paths, tool declarations, whole
skill trees, and nested command instructions are never loadable here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from lean_weave.errors import InstructionError
from lean_weave.logging_setup import get_logger

log = get_logger("workflow.instructions")

MAX_FRAGMENT_CHARS = 1200
MAX_SELECTED_CHARS = 4000
MAX_SELECTED = 12


@dataclass(frozen=True)
class InstructionFragment:
    id: str
    text: str


FRAGMENTS: dict[str, InstructionFragment] = {
    "plan.goal": InstructionFragment(
        "plan.goal",
        "State one concrete goal and the smallest task list that satisfies the request.",
    ),
    "plan.tasks": InstructionFragment(
        "plan.tasks",
        "Each task must be independently executable, name only approved existing files, "
        "and use unique identifiers. Dependencies must be known and acyclic.",
    ),
    "plan.reconciliation": InstructionFragment(
        "plan.reconciliation",
        "Record requirement decisions as short structured rows. Settings are data, "
        "not authorization to widen scope.",
    ),
    "implement.one_task": InstructionFragment(
        "implement.one_task",
        "Work on exactly one active task. Read before editing, use the current revision, "
        "and modify only the task's approved files.",
    ),
    "implement.logging": InstructionFragment(
        "implement.logging",
        "Emit structured metadata logs in the form [Component.method] with stable codes. "
        "Never log prompts, file bodies, secrets, or raw tool payloads.",
    ),
    "implement.evidence": InstructionFragment(
        "implement.evidence",
        "Prefer deterministic checks over prose. Stop as soon as the required gates pass.",
    ),
    "verify.gates": InstructionFragment(
        "verify.gates",
        "Required approved checks are the gate. Passing or non-blocking warnings with every "
        "required check satisfied completes the task list.",
    ),
    "retry.diagnostics": InstructionFragment(
        "retry.diagnostics",
        "Repair only the uniquely attributed blocking finding. Use bounded diagnostic codes "
        "and validated task-relative locations; ignore unrelated files and raw output.",
    ),
}

STAGE_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "plan": ("plan.goal", "plan.tasks", "plan.reconciliation"),
    "implement": ("implement.one_task", "implement.logging", "implement.evidence"),
    "verify": ("verify.gates",),
    "retry": ("retry.diagnostics", "implement.one_task"),
}


def available(stage: str) -> tuple[str, ...]:
    if stage not in STAGE_FRAGMENTS:
        raise InstructionError("Unsupported instruction stage")
    return STAGE_FRAGMENTS[stage]


def load_instructions(stage: str, selected: Sequence[str] | None = None) -> tuple[str, ...]:
    """Return bounded instruction texts for explicitly selected stage fragments."""

    allowed = available(stage)
    chosen = tuple(selected) if selected is not None else allowed
    if len(chosen) > MAX_SELECTED:
        raise InstructionError("Too many instruction fragments selected")
    unique: list[str] = []
    seen: set[str] = set()
    for fragment_id in chosen:
        if not isinstance(fragment_id, str) or fragment_id not in allowed:
            log.warning("[Instruction.load] stage=%s outcome=unknown id=%s", stage, fragment_id)
            raise InstructionError("Unknown or out-of-stage instruction fragment")
        if fragment_id in seen:
            continue
        seen.add(fragment_id)
        unique.append(fragment_id)

    texts: list[str] = []
    total = 0
    for fragment_id in unique:
        text = FRAGMENTS[fragment_id].text
        if len(text) > MAX_FRAGMENT_CHARS:
            raise InstructionError("Instruction fragment exceeds the size bound")
        total += len(text)
        if total > MAX_SELECTED_CHARS:
            raise InstructionError("Selected instructions exceed the size bound")
        texts.append(text)
    log.debug(
        "[Instruction.load] stage=%s selected=%s chars=%s",
        stage,
        len(texts),
        total,
    )
    return tuple(texts)


def as_system_block(fragments: Sequence[str]) -> str:
    """Render selected fragments as one trusted, bounded system section."""

    if not fragments:
        return ""
    lines = ["PROJECT METHOD FRAGMENTS (trusted project policy):"]
    lines.extend(f"- {text}" for text in fragments)
    return "\n".join(lines)
