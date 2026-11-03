"""Shared ``atlan/safeSwap`` builders for import findings (B008, P004, P005).

A swap is marked only for a module-level import, only when the
replacement is the same object, so applying it cannot change behaviour, and
only when ``find`` occurs exactly once on the finding's source line, so the
swap can be applied as written.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from conformance.suite.schema.extensions import SafeSwap, SafeSwapRequires

SDK_PACKAGE = "atlan-application-sdk"

#: Private module -> (public module re-exporting the same objects, first SDK version with them, names).
PUBLIC_REEXPORTS: dict[str, tuple[str, str, frozenset[str]]] = {
    "application_sdk.execution._temporal.activity_utils": (
        "application_sdk.execution",
        "3.0.0",
        frozenset({"build_output_path", "get_object_store_prefix"}),
    ),
}

PUBLIC_ALIAS_MODULE = "application_sdk.execution"

PUBLIC_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("temporalio.exceptions", "ActivityError"): ("TemporalActivityError", "3.21.0"),
    ("temporalio.exceptions", "ApplicationError"): (
        "TemporalApplicationError",
        "3.50.0",
    ),
    ("temporalio.exceptions", "CancelledError"): ("TemporalCancelledError", "3.21.0"),
    ("temporalio.exceptions", "ChildWorkflowError"): (
        "TemporalChildWorkflowError",
        "3.21.0",
    ),
    ("temporalio.exceptions", "TerminatedError"): ("TemporalTerminatedError", "3.21.0"),
    ("temporalio.exceptions", "TimeoutError"): ("TemporalTimeoutError", "3.21.0"),
    ("temporalio.client", "WorkflowFailureError"): (
        "TemporalWorkflowFailureError",
        "3.21.0",
    ),
}


def top_level_ids(tree: ast.AST) -> set[int]:
    """Ids of the module's top-level statements: the imports a swap may touch."""
    return {id(n) for n in getattr(tree, "body", [])}


_NEWLINE = re.compile(r"\r\n|\r|\n")


def source_lines(text: str) -> list[str]:
    """*text* split at the line breaks Python's tokenizer counts, so index ``lineno - 1`` is the node's line."""
    return _NEWLINE.split(text)


def _on_line(swap: SafeSwap, node: ast.stmt, lines: Sequence[str]) -> SafeSwap | None:
    """*swap* when its ``find`` occurs exactly once on *node*'s line, else ``None``.

    The same test an applier makes before replacing, so no marked swap is one
    the applier would have to refuse.
    """
    index = node.lineno - 1
    if 0 <= index < len(lines) and lines[index].count(swap.find) == 1:
        return swap
    return None


def reexport_swap(
    node: ast.ImportFrom, module: str, top_level: set[int], lines: Sequence[str]
) -> SafeSwap | None:
    entry = PUBLIC_REEXPORTS.get(module)
    if (
        id(node) not in top_level
        or entry is None
        or any(alias.name not in entry[2] for alias in node.names)
    ):
        return None
    public, since, _ = entry
    swap = SafeSwap(
        id="public-reexport",
        find=module,
        replace=public,
        requires=SafeSwapRequires(package=SDK_PACKAGE, min=since),
    )
    return _on_line(swap, node, lines)


def alias_swap(
    node: ast.stmt, top_level: set[int], lines: Sequence[str]
) -> SafeSwap | None:
    """A one-line, single-name import of a class with a public ``Temporal*`` alias.

    Marked only when ``<module> import <Name>`` is on the line exactly as
    written (one space each side of ``import``, no parentheses or tabs).
    """
    if (
        id(node) not in top_level
        or not isinstance(node, ast.ImportFrom)
        or len(node.names) != 1
        or node.lineno != node.end_lineno
    ):
        return None
    module = node.module or ""
    alias = node.names[0]
    entry = PUBLIC_ALIASES.get((module, alias.name))
    if entry is None:
        return None
    public, since = entry
    bound = "" if alias.asname else f" as {alias.name}"
    swap = SafeSwap(
        id="public-alias",
        find=f"{module} import {alias.name}",
        replace=f"{PUBLIC_ALIAS_MODULE} import {public}{bound}",
        requires=SafeSwapRequires(package=SDK_PACKAGE, min=since),
    )
    return _on_line(swap, node, lines)
