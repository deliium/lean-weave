"""AST-only Python symbol and import indexing. Modules are never imported."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Sequence

from lean_weave.context.models import SymbolKind, SymbolRef, normalize_relative_path
from lean_weave.context.policy import is_python
from lean_weave.context.repository import FileSnapshot, Repository
from lean_weave.logging_setup import get_logger

log = get_logger("context.python_index")


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


def _module_name(path: str) -> str:
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


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


def _collect(
    node: ast.AST,
    *,
    path: str,
    module: str,
    parent: str,
    in_class: bool,
    symbols: list[SymbolRef],
) -> None:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        local = f"{parent}.{node.name}" if parent else node.name
        kind = _kind(node, in_class=in_class)
        start, end = _span(node)
        symbols.append(
            SymbolRef(
                path=path,
                simple_name=node.name,
                qualified_name=f"{module}.{local}",
                kind=kind,
                start_line=start,
                end_line=end,
                signature=_signature(node),
                parent=parent or None,
            )
        )
        child_in_class = isinstance(node, ast.ClassDef)
        for child in ast.iter_child_nodes(node):
            _collect(
                child,
                path=path,
                module=module,
                parent=local,
                in_class=child_in_class,
                symbols=symbols,
            )
        return
    for child in ast.iter_child_nodes(node):
        _collect(
            child,
            path=path,
            module=module,
            parent=parent,
            in_class=in_class,
            symbols=symbols,
        )


def _raw_imports(tree: ast.AST, module: str, is_package: bool) -> list[tuple[str | None, int, list[str]]]:
    """Return ``(module, level, imported_names)`` records without resolving."""

    records: list[tuple[str | None, int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                records.append((alias.name, 0, []))
        elif isinstance(node, ast.ImportFrom):
            names = [(alias.name if alias.name != "*" else "*") for alias in node.names]
            records.append((node.module, node.level, names))
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


def parse_module(snapshot: FileSnapshot, *, module_paths: dict[str, str] | None = None) -> ParsedModule:
    module = _module_name(snapshot.path)
    try:
        tree = ast.parse(snapshot.text, filename=snapshot.path)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        log.debug("[Context.parse] status=error path=%s", snapshot.path)
        return ParsedModule(path=snapshot.path, module=module, error="syntax")
    symbols: list[SymbolRef] = []
    _collect(tree, path=snapshot.path, module=module, parent="", in_class=False, symbols=symbols)
    records = _raw_imports(tree, module, snapshot.path.endswith("__init__.py"))
    imports = _resolve_imports(records, module=module, module_paths=module_paths or {})
    log.debug(
        "[Context.parse] status=ok path=%s symbols=%s imports=%s", snapshot.path, len(symbols), len(imports)
    )
    return ParsedModule(
        path=snapshot.path,
        module=module,
        symbols=tuple(sorted(symbols, key=lambda item: (item.start_line, item.qualified_name))),
        imports=imports,
    )


def build_python_index(
    repository: Repository, paths: Iterable[str] | None = None
) -> PythonIndex:
    eligible = repository.inventory().files
    python_paths = [path for path in eligible if is_python(path)]
    module_paths: dict[str, str] = {}
    for path in python_paths:
        module_paths.setdefault(_module_name(path), path)
    index = PythonIndex(module_paths=module_paths)
    selected = sorted(set(python_paths if paths is None else (path for path in paths if is_python(path))))
    for path in selected:
        snapshot = repository.try_read(path)
        if snapshot is None:
            continue
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
