"""AST-only Python symbol and import indexing. Modules are never imported."""

from __future__ import annotations

import ast
import platform
import sys
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Callable, Iterable, Sequence

from lean_weave.context.models import SymbolKind, SymbolRef, normalize_relative_path
from lean_weave.context.policy import is_python
from lean_weave.context.repository import FileSnapshot, Repository
from lean_weave.logging_setup import get_logger

log = get_logger("context.python_index")

EXTRACTOR_VERSION = 1


def extractor_key() -> str:
    """Stable key for the AST extraction contract and interpreter generation."""

    implementation = platform.python_implementation().lower()
    return f"py-ast-v{EXTRACTOR_VERSION}-{implementation}-{sys.version_info.major}.{sys.version_info.minor}"


@dataclass(frozen=True)
class DefinitionFact:
    local_name: str
    simple_name: str
    kind: SymbolKind
    parent: str | None
    signature: str
    decorators: tuple[str, ...]
    start_line: int
    end_line: int
    traversal_order: int
    occurrence: int


@dataclass(frozen=True)
class ImportFact:
    kind: str
    module: str | None
    level: int
    names: tuple[tuple[str, str | None], ...]
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PythonFacts:
    status: str
    definitions: tuple[DefinitionFact, ...] = ()
    imports: tuple[ImportFact, ...] = ()
    line_count: int = 0


@dataclass(frozen=True)
class ParsedModule:
    path: str
    module: str
    symbols: tuple[SymbolRef, ...] = ()
    imports: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class PythonIndex:
    modules: dict[str, ParsedModule] = field(default_factory=dict)
    symbols: list[SymbolRef] = field(default_factory=list)
    import_edges: dict[str, tuple[str, ...]] = field(default_factory=dict)
    module_paths: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def lookup(self, name: str, *, path: str | None = None) -> list[SymbolRef]:
        if not name:
            return []
        target = normalize_relative_path(path) if path else None
        matches = [
            symbol
            for symbol in self.symbols
            if (target is None or symbol.path == target)
            and (
                symbol.simple_name == name
                or symbol.qualified_name == name
                or symbol.qualified_name.endswith(f".{name}")
            )
        ]
        return sorted(matches, key=lambda item: (item.path, item.start_line, item.qualified_name))

    def one_hop(self, paths: Iterable[str]) -> list[str]:
        targets: set[str] = set()
        for path in paths:
            targets.update(self.import_edges.get(path, ()))
        return sorted(targets)

    def symbols_for(self, path: str) -> list[SymbolRef]:
        return sorted(
            (symbol for symbol in self.symbols if symbol.path == path),
            key=lambda item: (item.start_line, item.qualified_name),
        )

    def module_for(self, path: str) -> str:
        module = self.modules.get(path)
        return module.module if module is not None else ""


def module_name_for_path(path: str) -> str:
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


_module_name = module_name_for_path


def _signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(base) for base in node.bases]
        keywords = [f"{item.arg}={ast.unparse(item.value)}" for item in node.keywords]
        suffix = f"({', '.join([*bases, *keywords])})" if bases or keywords else ""
        return f"class {node.name}{suffix}"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        arguments = ast.unparse(node.args)
        result = f"{prefix} {node.name}({arguments})"
        if node.returns is not None:
            result += f" -> {ast.unparse(node.returns)}"
        return result
    return getattr(node, "name", "")


def _span(node: ast.AST) -> tuple[int, int]:
    decorators = getattr(node, "decorator_list", ())
    starts = [getattr(item, "lineno", None) for item in decorators]
    start = min(
        value for value in [getattr(node, "lineno", 1), *starts] if isinstance(value, int)
    )
    end = getattr(node, "end_lineno", None) or getattr(node, "lineno", start)
    return start, max(start, int(end))


def _kind(node: ast.AST, *, in_class: bool) -> SymbolKind:
    if isinstance(node, ast.ClassDef):
        return SymbolKind.CLASS
    if isinstance(node, ast.AsyncFunctionDef):
        return SymbolKind.ASYNC_METHOD if in_class else SymbolKind.ASYNC_FUNCTION
    return SymbolKind.METHOD if in_class else SymbolKind.FUNCTION


def _decorators(node: ast.AST) -> tuple[str, ...]:
    result: list[str] = []
    for item in getattr(node, "decorator_list", ()):
        try:
            result.append(ast.unparse(item))
        except (ValueError, RecursionError):
            result.append("")
    return tuple(result)


def _collect_facts(
    node: ast.AST,
    *,
    parent: str,
    in_class: bool,
    definitions: list[DefinitionFact],
    counter: list[int],
) -> None:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        local = f"{parent}.{node.name}" if parent else node.name
        start, end = _span(node)
        definitions.append(
            DefinitionFact(
                local_name=local,
                simple_name=node.name,
                kind=_kind(node, in_class=in_class),
                parent=parent or None,
                signature=_signature(node),
                decorators=_decorators(node),
                start_line=start,
                end_line=end,
                traversal_order=counter[0],
                occurrence=0,
            )
        )
        counter[0] += 1
        child_in_class = isinstance(node, ast.ClassDef)
        for child in ast.iter_child_nodes(node):
            _collect_facts(
                child,
                parent=local,
                in_class=child_in_class,
                definitions=definitions,
                counter=counter,
            )
        return
    for child in ast.iter_child_nodes(node):
        _collect_facts(
            child,
            parent=parent,
            in_class=in_class,
            definitions=definitions,
            counter=counter,
        )


def _assign_occurrences(definitions: list[DefinitionFact]) -> list[DefinitionFact]:
    groups: dict[tuple[str, SymbolKind], list[DefinitionFact]] = {}
    for item in definitions:
        groups.setdefault((item.local_name, item.kind), []).append(item)
    assigned: dict[int, int] = {}
    for grouped in groups.values():
        for ordinal, item in enumerate(
            sorted(grouped, key=lambda fact: (fact.start_line, fact.end_line, fact.traversal_order))
        ):
            assigned[id(item)] = ordinal
    return [replace(item, occurrence=assigned[id(item)]) for item in definitions]


def _import_facts(tree: ast.AST) -> tuple[ImportFact, ...]:
    records: list[tuple[int, ImportFact]] = []
    for order, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Import):
            names = tuple((alias.name, alias.asname) for alias in node.names)
            records.append(
                (
                    order,
                    ImportFact(
                        kind="import",
                        module=None,
                        level=0,
                        names=names,
                        start_line=node.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    ),
                )
            )
        elif isinstance(node, ast.ImportFrom):
            names = tuple((alias.name, alias.asname) for alias in node.names)
            records.append(
                (
                    order,
                    ImportFact(
                        kind="from",
                        module=node.module,
                        level=node.level,
                        names=names,
                        start_line=node.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    ),
                )
            )
    records.sort(key=lambda item: (item[1].start_line, item[1].end_line, item[0]))
    return tuple(record for _order, record in records)


def extract_python_facts(snapshot: FileSnapshot) -> PythonFacts:
    """Extract path-independent Python facts; never returns source text."""

    line_count = snapshot.lines
    try:
        tree = ast.parse(snapshot.text, filename=snapshot.path)
    except (SyntaxError, ValueError):
        log.debug("[Context.parse] status=syntax")
        return PythonFacts(status="syntax", line_count=line_count)
    except (MemoryError, RecursionError):
        log.debug("[Context.parse] status=parse_limit")
        return PythonFacts(status="parse_limit", line_count=line_count)
    definitions: list[DefinitionFact] = []
    _collect_facts(tree, parent="", in_class=False, definitions=definitions, counter=[0])
    definitions = _assign_occurrences(definitions)
    facts = PythonFacts(
        status="ok",
        definitions=tuple(definitions),
        imports=_import_facts(tree),
        line_count=line_count,
    )
    log.debug(
        "[Context.parse] status=ok definitions=%s imports=%s",
        len(facts.definitions),
        len(facts.imports),
    )
    return facts


def _import_records(
    facts: PythonFacts,
) -> list[tuple[str | None, int, list[str]]]:
    records: list[tuple[str | None, int, list[str]]] = []
    for item in facts.imports:
        if item.kind == "import":
            for name, _alias in item.names:
                records.append((name, 0, []))
        else:
            records.append((item.module, item.level, [name for name, _alias in item.names]))
    return records


def _resolve_imports(
    records: Sequence[tuple[str | None, int, list[str]]],
    *,
    module: str,
    module_paths: dict[str, str],
) -> tuple[str, ...]:
    resolved: set[str] = set()
    package = module if module and module_paths.get(module, "").endswith("__init__.py") else module.rpartition(".")[0]
    for imported_module, level, names in records:
        candidates: list[str] = []
        if level:
            base_parts = package.split(".") if package else []
            keep = len(base_parts) - (level - 1)
            if keep <= 0:
                continue
            base = ".".join(base_parts[:keep])
            if imported_module:
                candidates.append(f"{base}.{imported_module}")
            else:
                candidates.append(base)
                candidates.extend(f"{base}.{name}" for name in names if name != "*")
        elif imported_module:
            candidates.append(imported_module)
            candidates.extend(f"{imported_module}.{name}" for name in names if name != "*")
        for candidate in candidates:
            target = module_paths.get(candidate)
            if target is not None:
                resolved.add(target)
    return tuple(sorted(resolved))


def project_python_facts(
    path: str,
    facts: PythonFacts,
    *,
    module_paths: dict[str, str] | None = None,
) -> ParsedModule:
    """Project path-independent facts into the existing V1 module shape."""

    module = module_name_for_path(path)
    if facts.status != "ok":
        return ParsedModule(path=path, module=module, error="syntax")
    symbols = [
        SymbolRef(
            path=path,
            simple_name=item.simple_name,
            qualified_name=f"{module}.{item.local_name}",
            kind=item.kind,
            start_line=item.start_line,
            end_line=item.end_line,
            signature=item.signature,
            parent=item.parent,
        )
        for item in facts.definitions
    ]
    imports = _resolve_imports(
        _import_records(facts), module=module, module_paths=module_paths or {}
    )
    return ParsedModule(
        path=path,
        module=module,
        symbols=tuple(sorted(symbols, key=lambda item: (item.start_line, item.qualified_name))),
        imports=imports,
    )


def parse_module(snapshot: FileSnapshot, *, module_paths: dict[str, str] | None = None) -> ParsedModule:
    facts = extract_python_facts(snapshot)
    return project_python_facts(snapshot.path, facts, module_paths=module_paths)


def build_python_index(
    repository: Repository,
    paths: Iterable[str] | None = None,
    *,
    load_facts: Callable[[FileSnapshot], PythonFacts] | None = None,
) -> PythonIndex:
    eligible = repository.inventory().files
    python_paths = [path for path in eligible if is_python(path)]
    candidates: dict[str, set[str]] = {}
    for path in python_paths:
        candidates.setdefault(module_name_for_path(path), set()).add(path)
    # Only unambiguous module names may participate in import resolution.
    module_paths = {
        module: min(paths)
        for module, paths in candidates.items()
        if len(paths) == 1
    }
    index = PythonIndex(module_paths=module_paths)
    selected = sorted(set(python_paths if paths is None else (path for path in paths if is_python(path))))
    for path in selected:
        snapshot = repository.try_read(path)
        if snapshot is None:
            continue
        if load_facts is not None:
            facts = load_facts(snapshot)
            parsed = project_python_facts(path, facts, module_paths=module_paths)
        else:
            parsed = parse_module(snapshot, module_paths=module_paths)
        index.modules[path] = parsed
        index.symbols.extend(parsed.symbols)
        index.import_edges[path] = parsed.imports
        if parsed.error:
            index.errors[path] = parsed.error
    index.symbols.sort(key=lambda item: (item.path, item.start_line, item.qualified_name))
    log.debug(
        "[Context.index] modules=%s symbols=%s errors=%s",
        len(index.modules),
        len(index.symbols),
        len(index.errors),
    )
    return index
