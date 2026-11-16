import typing

from lean_weave import contracts


def test_contracts_exported():
    names = [
        "CLI",
        "LLMProvider",
        "ContextProvider",
        "WorkflowOrchestrator",
        "CodeExecutor",
        "VerificationRunner",
        "StateStore",
        "MetricsCollector",
    ]
    for name in names:
        proto = getattr(contracts, name)
        assert typing.get_origin(proto) is None
        assert issubclass(proto, typing.Protocol)


def test_no_runtime_behavior_modules_import_cleanly():
    # Protocols should be importable without side effects beyond logging in schemas
    import lean_weave.contracts.llm_provider as lp

    assert hasattr(lp, "LLMProvider")
