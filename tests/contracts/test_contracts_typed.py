import inspect
import logging
from typing import Any, get_type_hints

from lean_weave.contracts import (
    CodeExecutor, ContextProvider, LLMProvider, MetricsCollector,
    StateStore, VerificationRunner, WorkflowOrchestrator,
)
from lean_weave.contracts._types import (
    ContextBundle, ExecRequest, ExecResult, LLMRequest, LLMResponse, StageName, TokenUsage,
)
from lean_weave.schemas import FeatureState, VerificationResult


class FakeLLM:
    def complete(self, request: LLMRequest) -> LLMResponse:
        return {"message": {"content": "offline"}, "usage": TokenUsage()}


class FakeContext:
    def build(self, stage: StageName, feature_state: FeatureState) -> ContextBundle:
        return {"stage": stage}


class FakeExecutor:
    def run(self, request: ExecRequest) -> ExecResult:
        return {"returncode": 0}


class FakeVerification:
    def run(self, feature_state: FeatureState) -> VerificationResult:
        return VerificationResult(status="pass")


class FakeStore:
    def load(self, feature_id: str) -> FeatureState:
        return FeatureState(feature_id=feature_id)

    def save(self, feature_state: FeatureState) -> None:
        self.saved = feature_state


class FakeMetrics:
    def record_usage(self, feature_id: str, stage: StageName, usage: TokenUsage) -> None:
        self.usage = usage

    def totals(self, feature_id: str) -> TokenUsage:
        return self.usage


class FakeOrchestrator:
    def run_feature(self, feature_id: str, request: str) -> FeatureState:
        return FeatureState(feature_id=feature_id, request=request)

    def resume(self, feature_id: str, decision: Any | None = None) -> FeatureState:
        return FeatureState(feature_id=feature_id)


def test_structural_calls():
    llm: LLMProvider = FakeLLM()
    context: ContextProvider = FakeContext()
    executor: CodeExecutor = FakeExecutor()
    verify: VerificationRunner = FakeVerification()
    store: StateStore = FakeStore()
    metrics: MetricsCollector = FakeMetrics()
    orchestrator: WorkflowOrchestrator = FakeOrchestrator()
    state = orchestrator.run_feature("f", "build")
    store.save(state)
    assert store.load("f").feature_id == "f"
    assert context.build("plan", state)["stage"] == "plan"
    assert executor.run({"argv": ["python"]})["returncode"] == 0
    assert verify.run(state).status == "pass"
    usage = llm.complete({})["usage"]
    metrics.record_usage("f", "plan", usage)
    assert metrics.totals("f") == usage
    for decision in (None, {"approve": True}, "approved"):
        assert orchestrator.resume("f", decision).feature_id == "f"
    assert orchestrator.resume("f").feature_id == "f"
    for proto, fake in [
        (LLMProvider, llm), (ContextProvider, context), (CodeExecutor, executor),
        (VerificationRunner, verify), (StateStore, store),
        (MetricsCollector, metrics), (WorkflowOrchestrator, orchestrator),
    ]:
        for method_name, method in vars(proto).items():
            if method_name.startswith("_") or not callable(method):
                continue
            assert get_type_hints(getattr(type(fake), method_name)) == get_type_hints(method)
        logging.getLogger("lean_weave.tests").debug(
            "[Contracts.verify] protocol=%s ok=true fake=%s", proto.__name__, type(fake).__name__
        )


def test_signature_regressions():
    method = WorkflowOrchestrator.resume
    assert list(inspect.signature(method).parameters) == ["self", "feature_id", "decision"]
    assert inspect.signature(method).parameters["decision"].default is None
    assert get_type_hints(method) == {
        "feature_id": str, "decision": Any | None, "return": FeatureState,
    }
    assert get_type_hints(StateStore.load)["return"] is FeatureState
    assert get_type_hints(VerificationRunner.run)["return"] is VerificationResult
