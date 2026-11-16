from deepseek_probe import assert_fixture_shapes, run_probe


def test_offline_fixture_shapes_pass():
    results = assert_fixture_shapes()
    assert results
    assert all(r["ok"] for r in results)


def test_run_probe_offline_by_default(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    out = run_probe()
    assert out["mode"] == "offline"
    assert out["ok"] is True
    assert out["matrix_notes"]["tools_require_reasoning_echo"] is True
