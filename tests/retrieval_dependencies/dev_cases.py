"""Original development fixtures and frozen case metadata.

Names, layouts and queries are invented for this suite. They are independent of
known holdout repositories and must stay out of product ranking inputs.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from lean_weave.config.models import ContextConfig, PackingConfig
from lean_weave.context.models import content_digest
from lean_weave.logging_setup import get_logger

log = get_logger("tests.retrieval_dependencies.dev_cases")

Classification = Literal["coverage", "uncertainty", "budget", "control"]
IntervalRole = Literal["anchor", "definition", "import", "negative"]

COMMON_MODEL = "deepseek-flash"
COMMON_OUTPUT_RESERVE = 1024
BASELINE_REVISION = "91fc0724e5eecc6af0fa519c50702b977b4bdaf9"

# Filler keeps every definition from fitting under a tight budget.
_FILLER = "\n".join(f"PAD_ROW_{index} = {index}" for index in range(60))


@dataclass(frozen=True)
class EvidenceInterval:
    path: str
    start_line: int
    end_line: int
    role: IntervalRole
    note: str = ""


@dataclass(frozen=True)
class ExpansionScript:
    """Evaluator-only L2 opt-in; never passed into PackRequest."""

    enabled: bool = False
    max_attempts: int = 2


@dataclass(frozen=True)
class BudgetProfile:
    name: str
    v1_stage_prompt_tokens_max: int
    v1_total_code_chars_max: int
    v1_per_file_chars_max: int
    v2_total_tokens: int
    v2_source_tokens: int
    v2_map_tokens: int
    v2_system_tokens: int
    v2_task_tokens: int
    v2_tool_tokens: int
    v2_output_tokens: int
    v2_max_expansions: int
    v2_cumulative_source_tokens: int


@dataclass(frozen=True)
class DevCase:
    case_id: str
    classification: Classification
    files: dict[str, str]
    task_query: str
    edit_target: str
    task_title: str
    task_description: str
    anchors: tuple[EvidenceInterval, ...]
    must_cover: tuple[EvidenceInterval, ...]
    negatives: tuple[EvidenceInterval, ...]
    profile: str
    expansion: ExpansionScript = ExpansionScript()


PROFILES: dict[str, BudgetProfile] = {
    "standard": BudgetProfile(
        name="standard",
        v1_stage_prompt_tokens_max=6000,
        v1_total_code_chars_max=40000,
        v1_per_file_chars_max=12000,
        v2_total_tokens=8192,
        v2_source_tokens=4096,
        v2_map_tokens=512,
        v2_system_tokens=1024,
        v2_task_tokens=1024,
        v2_tool_tokens=2048,
        v2_output_tokens=COMMON_OUTPUT_RESERVE,
        v2_max_expansions=2,
        v2_cumulative_source_tokens=8192,
    ),
    "tight": BudgetProfile(
        name="tight",
        v1_stage_prompt_tokens_max=180,
        v1_total_code_chars_max=240,
        v1_per_file_chars_max=240,
        v2_total_tokens=900,
        v2_source_tokens=120,
        v2_map_tokens=80,
        v2_system_tokens=120,
        v2_task_tokens=120,
        v2_tool_tokens=80,
        v2_output_tokens=200,
        v2_max_expansions=1,
        v2_cumulative_source_tokens=200,
    ),
}


def v1_context_config(profile: BudgetProfile) -> ContextConfig:
    return ContextConfig(
        stage_prompt_tokens_max=profile.v1_stage_prompt_tokens_max,
        total_code_chars_max=profile.v1_total_code_chars_max,
        per_file_chars_max=profile.v1_per_file_chars_max,
        digest_chars_max=2000,
    )


def v2_packing_config(profile: BudgetProfile) -> PackingConfig:
    return PackingConfig(
        total_tokens=profile.v2_total_tokens,
        source_tokens=profile.v2_source_tokens,
        map_tokens=profile.v2_map_tokens,
        system_tokens=profile.v2_system_tokens,
        task_tokens=profile.v2_task_tokens,
        tool_tokens=profile.v2_tool_tokens,
        output_tokens=profile.v2_output_tokens,
        max_expansions=profile.v2_max_expansions,
        cumulative_source_tokens=profile.v2_cumulative_source_tokens,
    )


def _symbol_interval(
    source: str,
    path: str,
    name: str,
    *,
    role: IntervalRole = "definition",
    occurrence: int = 0,
    note: str = "",
) -> EvidenceInterval:
    tree = ast.parse(source)
    matches: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                matches.append(node)
    if occurrence >= len(matches):
        raise ValueError(f"symbol {name!r} occurrence {occurrence} missing in {path}")
    node = matches[occurrence]
    end = getattr(node, "end_lineno", None) or node.lineno
    return EvidenceInterval(
        path=path,
        start_line=int(node.lineno),
        end_line=int(end),
        role=role,
        note=note or name,
    )


def _import_interval(
    source: str,
    path: str,
    needle: str,
    *,
    note: str = "",
) -> EvidenceInterval:
    for index, line in enumerate(source.splitlines(), start=1):
        if needle in line and ("import" in line or "from " in line):
            return EvidenceInterval(
                path=path,
                start_line=index,
                end_line=index,
                role="import",
                note=note or needle,
            )
    raise ValueError(f"import containing {needle!r} missing in {path}")


def _build_cases() -> tuple[DevCase, ...]:
    # (a) imported class used in call + annotation
    widget = (
        "class HarborWidget:\n"
        "    \"\"\"Stable widget used by the runner.\"\"\"\n"
        "\n"
        "    def spin(self) -> int:\n"
        "        return 41\n"
    )
    runner = (
        "from models.widget import HarborWidget\n"
        "\n"
        "\n"
        "def activate(item: HarborWidget) -> int:\n"
        "    return item.spin()\n"
        "\n"
        "\n"
        f"{_FILLER}\n"
    )
    filler_a = f"def unused_harbor_noise():\n    return 0\n\n{_FILLER}\n"

    # (b) aliased from-import, dotted module import, unused import
    lantern = "def glow() -> int:\n    return 3\n\n"
    mesa = "def value() -> int:\n    return 5\n\n"
    clutter = "def clutter() -> int:\n    return 99\n\n"
    paint = (
        "from tools.lantern import glow as shine\n"
        "import tools.mesa as mesa_mod\n"
        "from tools.unused import clutter\n"
        "\n"
        "\n"
        "def paint() -> int:\n"
        "    return shine() + mesa_mod.value()\n"
        "\n"
        "\n"
        f"{_FILLER}\n"
    )

    # (c) relative two-hop chain
    leaf = "def leaf_value() -> int:\n    return 7\n\n"
    mid = (
        "from .leaf import leaf_value\n"
        "\n"
        "\n"
        "def mid_value() -> int:\n"
        "    return leaf_value()\n"
        "\n"
    )
    top = (
        "from .mid import mid_value\n"
        "\n"
        "\n"
        "def top_value() -> int:\n"
        "    return mid_value()\n"
        "\n"
        "\n"
        f"{_FILLER}\n"
    )

    # (d) same-file helper + nested shadowing
    shadow = (
        "def helper() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def outer() -> int:\n"
        "    def helper() -> int:\n"
        "        return 2\n"
        "\n"
        "    return helper()\n"
        "\n"
        "\n"
        f"{_FILLER}\n"
    )

    # (e) circular dependency
    cycle_a = (
        "import cycle_b\n"
        "\n"
        "\n"
        "def a_value() -> int:\n"
        "    return cycle_b.b_value()\n"
        "\n"
    )
    cycle_b = (
        "import cycle_a\n"
        "\n"
        "\n"
        "def b_value() -> int:\n"
        "    return 11\n"
        "\n"
        "\n"
        f"{_FILLER}\n"
    )

    # (f) ambiguity / wildcard / dynamic negatives
    twin_one = "class Twin:\n    tag = 'one'\n\n"
    twin_two = "class Twin:\n    tag = 'two'\n\n"
    wild = "from twins.one import *\n\n"
    dynamic = (
        "def load_name(module_name: str, attr: str):\n"
        "    return getattr(__import__(module_name), attr)\n"
        "\n"
    )
    consumer = (
        "from twins.one import Twin\n"
        "\n"
        "\n"
        "def use_twin(item: Twin) -> str:\n"
        "    return item.tag\n"
        "\n"
    )

    # (g) large proven requirement under tight budget
    huge_body = "\n".join(f"        self.slot_{index} = {index}" for index in range(120))
    huge_def = (
        "class MassivePayload:\n"
        "    def __init__(self) -> None:\n"
        f"{huge_body}\n"
        "\n"
        "    def fingerprint(self) -> int:\n"
        "        return len(self.__dict__)\n"
        "\n"
    )
    huge_use = (
        "from bulk.payload import MassivePayload\n"
        "\n"
        "\n"
        "def stamp() -> int:\n"
        "    return MassivePayload().fingerprint()\n"
        "\n"
    )

    # (h) no-dependency control + expandable optional target
    alone = (
        "def solo_mark() -> str:\n"
        "    return 'ok'\n"
        "\n"
    )
    optional_lib = (
        "def optional_hint() -> str:\n"
        "    return 'hint'\n"
        "\n"
        "\n"
        "def decoy_noise() -> str:\n"
        "    return 'noise'\n"
        "\n"
        f"{_FILLER}\n"
    )
    optional_use = (
        "def bridge() -> str:\n"
        "    return 'bridge'\n"
        "\n"
        "\n"
        "# optional_hint lives in optional/lib.py for explicit expansion only\n"
    )

    cases = (
        DevCase(
            case_id="imported_class_call_annotation",
            classification="coverage",
            files={
                "models/__init__.py": "",
                "models/widget.py": widget,
                "service/__init__.py": "",
                "service/runner.py": runner,
                "service/noise.py": filler_a,
            },
            task_query="adjust HarborWidget activation return path",
            edit_target="service/runner.py",
            task_title="Activate harbor widget",
            task_description="Adjust HarborWidget activation in service/runner.py",
            anchors=(
                _symbol_interval(runner, "service/runner.py", "activate", role="anchor"),
            ),
            must_cover=(
                _symbol_interval(widget, "models/widget.py", "HarborWidget"),
                _import_interval(runner, "service/runner.py", "HarborWidget"),
            ),
            negatives=(
                _symbol_interval(filler_a, "service/noise.py", "unused_harbor_noise", role="negative"),
            ),
            profile="standard",
        ),
        DevCase(
            case_id="aliased_and_unused_imports",
            classification="coverage",
            files={
                "tools/__init__.py": "",
                "tools/lantern.py": lantern,
                "tools/mesa.py": mesa,
                "tools/unused.py": clutter,
                "studio/__init__.py": "",
                "studio/paint.py": paint,
            },
            task_query="revise paint composition using shine and mesa",
            edit_target="studio/paint.py",
            task_title="Revise paint composition",
            task_description="Revise paint composition in studio/paint.py",
            anchors=(_symbol_interval(paint, "studio/paint.py", "paint", role="anchor"),),
            must_cover=(
                _symbol_interval(lantern, "tools/lantern.py", "glow"),
                _symbol_interval(mesa, "tools/mesa.py", "value"),
                _import_interval(paint, "studio/paint.py", "glow as shine"),
                _import_interval(paint, "studio/paint.py", "tools.mesa"),
            ),
            negatives=(
                _symbol_interval(clutter, "tools/unused.py", "clutter", role="negative"),
                _import_interval(paint, "studio/paint.py", "clutter", note="unused_import"),
            ),
            profile="standard",
        ),
        DevCase(
            case_id="relative_two_hop_chain",
            classification="coverage",
            files={
                "pkg/__init__.py": "",
                "pkg/leaf.py": leaf,
                "pkg/mid.py": mid,
                "pkg/top.py": top,
            },
            task_query="update top_value relative import chain",
            edit_target="pkg/top.py",
            task_title="Update top value",
            task_description="Update top_value in pkg/top.py",
            anchors=(_symbol_interval(top, "pkg/top.py", "top_value", role="anchor"),),
            must_cover=(
                _symbol_interval(mid, "pkg/mid.py", "mid_value"),
                _symbol_interval(leaf, "pkg/leaf.py", "leaf_value"),
                _import_interval(top, "pkg/top.py", "mid_value"),
                _import_interval(mid, "pkg/mid.py", "leaf_value"),
            ),
            negatives=(),
            profile="standard",
        ),
        DevCase(
            case_id="same_file_shadow_helper",
            classification="coverage",
            files={"nest/__init__.py": "", "nest/shadow.py": shadow},
            task_query="clarify outer helper shadowing behavior",
            edit_target="nest/shadow.py",
            task_title="Clarify shadow helper",
            task_description="Clarify outer helper shadowing in nest/shadow.py",
            anchors=(_symbol_interval(shadow, "nest/shadow.py", "outer", role="anchor"),),
            must_cover=(
                _symbol_interval(shadow, "nest/shadow.py", "helper", occurrence=0),
                _symbol_interval(shadow, "nest/shadow.py", "helper", occurrence=1),
            ),
            negatives=(),
            profile="standard",
        ),
        DevCase(
            case_id="circular_module_pair",
            classification="coverage",
            files={"cycle_a.py": cycle_a, "cycle_b.py": cycle_b},
            task_query="stabilize circular a_value and b_value pair",
            edit_target="cycle_a.py",
            task_title="Stabilize cycle pair",
            task_description="Stabilize circular pair starting at cycle_a.py",
            anchors=(_symbol_interval(cycle_a, "cycle_a.py", "a_value", role="anchor"),),
            must_cover=(
                _symbol_interval(cycle_b, "cycle_b.py", "b_value"),
                _import_interval(cycle_a, "cycle_a.py", "cycle_b"),
            ),
            negatives=(),
            profile="standard",
        ),
        DevCase(
            case_id="ambiguous_wildcard_dynamic",
            classification="uncertainty",
            files={
                "twins/__init__.py": "",
                "twins/one.py": twin_one,
                "twins/two.py": twin_two,
                "wild.py": wild,
                "dynamic.py": dynamic,
                "consumer.py": consumer,
            },
            task_query="inspect Twin consumer without guessing duplicates",
            edit_target="consumer.py",
            task_title="Inspect twin consumer",
            task_description="Inspect Twin consumer in consumer.py",
            anchors=(_symbol_interval(consumer, "consumer.py", "use_twin", role="anchor"),),
            must_cover=(
                _symbol_interval(twin_one, "twins/one.py", "Twin"),
                _import_interval(consumer, "consumer.py", "Twin"),
            ),
            negatives=(
                _symbol_interval(twin_two, "twins/two.py", "Twin", role="negative", note="duplicate"),
                EvidenceInterval(
                    path="wild.py", start_line=1, end_line=1, role="negative", note="wildcard"
                ),
                _symbol_interval(dynamic, "dynamic.py", "load_name", role="negative"),
            ),
            profile="standard",
        ),
        DevCase(
            case_id="budget_exhaustion_large_def",
            classification="budget",
            files={
                "bulk/__init__.py": "",
                "bulk/payload.py": huge_def,
                "bulk/stamp.py": huge_use,
            },
            task_query="stamp MassivePayload fingerprint under tight budget",
            edit_target="bulk/stamp.py",
            task_title="Stamp massive payload",
            task_description="Stamp MassivePayload fingerprint in bulk/stamp.py",
            anchors=(_symbol_interval(huge_use, "bulk/stamp.py", "stamp", role="anchor"),),
            must_cover=(
                _symbol_interval(huge_def, "bulk/payload.py", "MassivePayload"),
                _import_interval(huge_use, "bulk/stamp.py", "MassivePayload"),
            ),
            negatives=(),
            profile="tight",
        ),
        DevCase(
            case_id="control_no_dependency",
            classification="control",
            files={"alone.py": alone},
            task_query="keep solo_mark behavior unchanged",
            edit_target="alone.py",
            task_title="Keep solo mark",
            task_description="Keep solo_mark behavior in alone.py",
            anchors=(_symbol_interval(alone, "alone.py", "solo_mark", role="anchor"),),
            must_cover=(),
            negatives=(),
            profile="standard",
        ),
        DevCase(
            case_id="control_optional_expansion",
            classification="control",
            files={
                "optional/__init__.py": "",
                "optional/lib.py": optional_lib,
                "optional/bridge.py": optional_use,
            },
            task_query="bridge entrypoint with optional hint available",
            edit_target="optional/bridge.py",
            task_title="Bridge with optional hint",
            task_description="Bridge entrypoint in optional/bridge.py",
            anchors=(_symbol_interval(optional_use, "optional/bridge.py", "bridge", role="anchor"),),
            must_cover=(),
            negatives=(
                _symbol_interval(
                    optional_lib, "optional/lib.py", "decoy_noise", role="negative"
                ),
            ),
            profile="standard",
            expansion=ExpansionScript(enabled=True, max_attempts=2),
        ),
    )
    log.debug(
        "[DevCases] built cases=%s classifications=%s",
        len(cases),
        sorted({case.classification for case in cases}),
    )
    return cases


DEV_CASES: tuple[DevCase, ...] = _build_cases()


def case_file_digests(case: DevCase) -> dict[str, str]:
    return {path: content_digest(text) for path, text in sorted(case.files.items())}


def fixture_manifest() -> dict:
    """Stable label/fixture identity for freeze and comparability checks."""

    payload = {
        "baseline_revision": BASELINE_REVISION,
        "model": COMMON_MODEL,
        "output_reserve": COMMON_OUTPUT_RESERVE,
        "profiles": {
            name: asdict(profile) for name, profile in sorted(PROFILES.items())
        },
        "cases": [
            {
                "case_id": case.case_id,
                "classification": case.classification,
                "task_query": case.task_query,
                "edit_target": case.edit_target,
                "task_title": case.task_title,
                "task_description": case.task_description,
                "profile": case.profile,
                "expansion": asdict(case.expansion),
                "files": case.files,
                "file_digests": case_file_digests(case),
                "anchors": [asdict(item) for item in case.anchors],
                "must_cover": [asdict(item) for item in case.must_cover],
                "negatives": [asdict(item) for item in case.negatives],
            }
            for case in DEV_CASES
        ],
    }
    return payload


def fixture_hash() -> str:
    encoded = json.dumps(
        fixture_manifest(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    log.debug("[DevCases] fixture_hash=%s cases=%s", digest, len(DEV_CASES))
    return digest


def get_case(case_id: str) -> DevCase:
    for case in DEV_CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(case_id)
