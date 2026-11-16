"""LangGraph StateGraph interrupt / resume / durable checkpoint spike."""

from __future__ import annotations

import operator
import tempfile
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from lean_weave_spike_logging import configure_spike_logging

log = configure_spike_logging()


class ProbeState(TypedDict):
    steps: Annotated[list[str], operator.add]
    value: int
    fail_once: bool


def _node_prepare(state: ProbeState) -> dict[str, Any]:
    log.debug(
        "[LangGraphProbe.step] prepare {thread_id=n/a, event=prepare} value=%s",
        state.get("value"),
    )
    return {"steps": ["prepare"], "value": state.get("value", 0) + 1}


def _node_human_gate(state: ProbeState) -> dict[str, Any]:
    decision = interrupt({"prompt": "approve?", "value": state.get("value")})
    log.info(
        "[LangGraphProbe.step] interrupt hit {thread_id=n/a, event=interrupt} decision=%s",
        decision,
    )
    return {"steps": [f"gate:{decision}"], "value": state.get("value", 0)}


def _node_work(state: ProbeState) -> dict[str, Any]:
    if state.get("fail_once"):
        # Flip flag via returned state is not enough mid-raise; use mutable sentinel on first call
        raise RuntimeError("intentional probe failure")
    return {"steps": ["work"], "value": state.get("value", 0) + 10}


def _node_done(state: ProbeState) -> dict[str, Any]:
    return {"steps": ["done"], "value": state.get("value", 0)}


def build_graph(checkpointer: Any):
    g = StateGraph(ProbeState)
    g.add_node("prepare", _node_prepare)
    g.add_node("human_gate", _node_human_gate)
    g.add_node("work", _node_work)
    g.add_node("done", _node_done)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "human_gate")
    g.add_edge("human_gate", "work")
    g.add_edge("work", "done")
    g.add_edge("done", END)
    compiled = g.compile(checkpointer=checkpointer)
    log.info(
        "[LangGraphProbe.step] graph compile {thread_id=n/a, event=compile} checkpointer=%s",
        type(checkpointer).__name__,
    )
    return compiled


def demo_interrupt_resume(thread_id: str = "lg-interrupt-1") -> dict[str, Any]:
    saver = InMemorySaver()
    app = build_graph(saver)
    config = {"configurable": {"thread_id": thread_id}}

    interrupted = app.invoke({"steps": [], "value": 0, "fail_once": False}, config)
    log.info(
        "[LangGraphProbe.step] after first invoke {thread_id=%s, event=interrupted} state=%s",
        thread_id,
        interrupted,
    )
    # Resume with approval
    final = app.invoke(Command(resume="approve"), config)
    log.info(
        "[LangGraphProbe.step] resume complete {thread_id=%s, event=resume} steps=%s value=%s",
        thread_id,
        final.get("steps"),
        final.get("value"),
    )
    return {"thread_id": thread_id, "checkpointer": "InMemorySaver", "final": final}


def demo_continue_after_error(thread_id: str = "lg-error-1") -> dict[str, Any]:
    """Demonstrate continue-after-error via invoke(None, config).

    Node restart semantics: the failed node re-runs from the last checkpoint;
    completed upstream nodes are not re-executed for the same thread.
    """

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, state: ProbeState) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("intentional probe failure")
            return {"steps": ["work"], "value": state.get("value", 0) + 10}

    flaky = Flaky()
    saver = InMemorySaver()
    g = StateGraph(ProbeState)
    g.add_node("prepare", _node_prepare)
    g.add_node("human_gate", _node_human_gate)
    g.add_node("work", flaky)
    g.add_node("done", _node_done)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "human_gate")
    g.add_edge("human_gate", "work")
    g.add_edge("work", "done")
    g.add_edge("done", END)
    app = g.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": thread_id}}

    app.invoke({"steps": [], "value": 0, "fail_once": False}, config)
    try:
        app.invoke(Command(resume="approve"), config)
        raise AssertionError("expected work node to fail on first attempt")
    except RuntimeError as exc:
        err = str(exc)
        log.error(
            "[LangGraphProbe.step] work failed {thread_id=%s, event=error} err=%s",
            thread_id,
            err,
        )
        final = app.invoke(None, config)
        log.info(
            "[LangGraphProbe.step] continue-after-error {thread_id=%s, event=resume} steps=%s calls=%s",
            thread_id,
            final.get("steps"),
            flaky.calls,
        )
        return {
            "thread_id": thread_id,
            "error": err,
            "final": final,
            "work_calls": flaky.calls,
            "note": "failed node re-executed; prepare/gate not duplicated beyond checkpoint replay rules",
        }


def demo_sqlite_durable_resume(
    db_path: str | Path | None = None,
    thread_id: str = "lg-sqlite-1",
) -> dict[str, Any]:
    """Persist interrupt across 'process restart' (new SqliteSaver + same file)."""
    if db_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        db_path = tmp.name
        tmp.close()
    db_path = str(db_path)

    # Process 1: run until interrupt
    with SqliteSaver.from_conn_string(db_path) as saver:
        app = build_graph(saver)
        config = {"configurable": {"thread_id": thread_id}}
        app.invoke({"steps": [], "value": 1, "fail_once": False}, config)
        snap = app.get_state(config)
        log.debug(
            "[LangGraphProbe.step] checkpoint metadata {thread_id=%s, event=checkpoint} next=%s",
            thread_id,
            snap.next,
        )
        log.info(
            "[LangGraphProbe.step] sqlite interrupt persisted {thread_id=%s, event=interrupt} checkpointer=SqliteSaver path=%s",
            thread_id,
            db_path,
        )

    # Process 2: new saver instance, same file + thread_id
    with SqliteSaver.from_conn_string(db_path) as saver2:
        app2 = build_graph(saver2)
        config = {"configurable": {"thread_id": thread_id}}
        final = app2.invoke(Command(resume="approve"), config)
        log.info(
            "[LangGraphProbe.step] sqlite resume complete {thread_id=%s, event=resume} steps=%s",
            thread_id,
            final.get("steps"),
        )
        return {
            "thread_id": thread_id,
            "db_path": db_path,
            "checkpointer": "SqliteSaver",
            "final": final,
        }


if __name__ == "__main__":
    print(demo_interrupt_resume())
    print(demo_continue_after_error())
    print(demo_sqlite_durable_resume())
