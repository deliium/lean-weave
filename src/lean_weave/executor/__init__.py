"""Standalone, LLM-free code reading and editing tools.

These functions do not start a workflow, open a provider, or register CLI
commands. Callers pass an explicit workspace root and a trusted approval
snapshot. YAML alone never grants writes or process execution.
"""

from lean_weave.executor.approval import ToolApproval, approve_tools
from lean_weave.executor.git_diff import inspect_git_diff
from lean_weave.executor.patch import apply_patch
from lean_weave.executor.source import read_range, read_symbol, search_symbols

__all__ = [
    "ToolApproval",
    "apply_patch",
    "approve_tools",
    "inspect_git_diff",
    "read_range",
    "read_symbol",
    "search_symbols",
]
