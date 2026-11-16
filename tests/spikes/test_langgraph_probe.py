import tempfile
from pathlib import Path

from langgraph_probe import (
    demo_continue_after_error,
    demo_interrupt_resume,
    demo_sqlite_durable_resume,
)


def test_interrupt_resume_inmemory():
    result = demo_interrupt_resume(thread_id="test-interrupt")
    final = result["final"]
    assert "prepare" in final["steps"]
    assert "gate:approve" in final["steps"]
    assert "work" in final["steps"]
    assert "done" in final["steps"]
    assert final["value"] == 11  # 0+1 prepare, +10 work


def test_continue_after_error():
    result = demo_continue_after_error(thread_id="test-error")
    assert result["error"]
    assert result["work_calls"] == 2
    assert "done" in result["final"]["steps"]


def test_sqlite_durable_resume_across_saver_instances():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ckpt.sqlite"
        result = demo_sqlite_durable_resume(db_path=db, thread_id="test-sqlite")
        assert result["checkpointer"] == "SqliteSaver"
        assert "done" in result["final"]["steps"]
        assert db.exists()
