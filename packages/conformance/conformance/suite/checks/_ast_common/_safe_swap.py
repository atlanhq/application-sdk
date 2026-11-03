"""Shared ``atlan/safeSwap`` builders for import findings (B008, P004, P005).

A swap is marked only for a module-level import, and only when the
replacement is the same object, so applying it cannot change behaviour.
"""

from __future__ import annotations

import ast

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


def reexport_swap(
    node: ast.ImportFrom, module: str, top_level: set[int]
) -> SafeSwap | None:
    entry = PUBLIC_REEXPORTS.get(module)
    if (
        id(node) not in top_level
        or entry is None
        or any(alias.name not in entry[2] for alias in node.names)
    ):
        return None
    public, since, _ = entry
    return SafeSwap(
        id="public-reexport",
        find=module,
        replace=public,
        requires=SafeSwapRequires(package=SDK_PACKAGE, min=since),
    )


def alias_swap(node: ast.stmt, top_level: set[int]) -> SafeSwap | None:
    """A one-line, single-name import of a class with a public ``Temporal*`` alias.

    Marked only when the line is written in the plain form
    ``from <module> import <Name>`` (one space each, no parentheses), checked
    from the name's column, so ``find`` is on the line exactly as written.
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
    prefix = f"from {module} import "
    if getattr(alias, "col_offset", None) != node.col_offset + len(prefix):
        return None
    public, since = entry
    bound = "" if alias.asname else f" as {alias.name}"
    return SafeSwap(
        id="public-alias",
        find=f"{module} import {alias.name}",
        replace=f"{PUBLIC_ALIAS_MODULE} import {public}{bound}",
        requires=SafeSwapRequires(package=SDK_PACKAGE, min=since),
    )
