"""Collect ``@entrypoint`` wire names from Python source files via AST.

For each module we:

1. Identify the local names that are bound to ``application_sdk``'s
   ``entrypoint`` decorator via import statements.
2. Walk every function / async-function definition, detect those decorated
   with one of those names, and derive the wire name:

   * ``@entrypoint`` (bare) → ``method_name`` with ``_`` → ``-``
   * ``@entrypoint(name="literal")`` → the literal
   * ``@entrypoint(name=<non-literal>)`` → recorded as *unresolved* (cannot be
     statically verified)

3. Separately collect ``App`` subclass ``ClassDef`` nodes so contract-only
   findings can be anchored to a meaningful location in the code.

Import-provenance policy
------------------------
A file only contributes ``@entrypoint`` findings when it imports ``entrypoint``
from ``application_sdk.*`` (any sub-module, any alias).  This matches how the
other P-series checks handle SDK import provenance and avoids false positives
from third-party decorators that happen to be named ``entrypoint``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

_SDK_PREFIX = "application_sdk"


# ---------------------------------------------------------------------------
# Import-provenance helpers
# ---------------------------------------------------------------------------


def _sdk_entrypoint_aliases(tree: ast.Module) -> frozenset[str]:
    """Return local names bound to the SDK ``entrypoint`` decorator in this module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level > 0:
            continue
        module = node.module or ""
        if module == _SDK_PREFIX or module.startswith(_SDK_PREFIX + "."):
            for alias in node.names:
                if alias.name == "entrypoint":
                    bound.add(alias.asname or alias.name)
    return frozenset(bound)


def _sdk_app_aliases(tree: ast.Module) -> frozenset[str]:
    """Return local names bound to the SDK ``App`` class in this module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level > 0:
            continue
        module = node.module or ""
        if module == _SDK_PREFIX or module.startswith(_SDK_PREFIX + "."):
            for alias in node.names:
                if alias.name == "App":
                    bound.add(alias.asname or alias.name)
    return frozenset(bound)


# ---------------------------------------------------------------------------
# Wire-name extraction
# ---------------------------------------------------------------------------


def _method_name_to_kebab(name: str) -> str:
    """``'extract_metadata'`` → ``'extract-metadata'`` (mirrors ``application_sdk.app.entrypoint``)."""
    return name.replace("_", "-")


def _extract_ep_name(
    deco: ast.expr,
    method_name: str,
) -> tuple[str | None, bool]:
    """Parse a decorator expression for its entry-point wire name.

    Returns
    -------
    (name, is_unresolved):
        ``name``          – the wire name, or ``None`` if this is not an entry-point decorator.
        ``is_unresolved`` – ``True`` when ``name=`` is present but not a string literal.
    """
    if isinstance(deco, ast.Name):
        # @entrypoint  (bare)
        return _method_name_to_kebab(method_name), False

    if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
        # @entrypoint(...)
        for kw in deco.keywords:
            if kw.arg == "name":
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str
                ):
                    return kw.value.value, False
                # Non-literal name= (variable, f-string, …)
                return None, True
        # @entrypoint(default=True) or other kwargs — no name= → use method name
        return _method_name_to_kebab(method_name), False

    return None, False


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class EntrypointLocation:
    """A single ``@entrypoint``-decorated method found in the codebase."""

    name: str
    """Wire name (kebab-case)."""
    filename: str
    """Path relative to the repo root."""
    node: ast.AST
    """The ``@entrypoint`` decorator node — anchored here so ``# conformance: ignore``
    on the line directly above the decorator suppresses the finding correctly."""


@dataclass
class AppClassLocation:
    """An ``App`` subclass found in the codebase."""

    filename: str
    node: ast.ClassDef


@dataclass
class UnresolvedLocation:
    """An ``@entrypoint`` whose ``name=`` value could not be statically resolved."""

    filename: str
    node: ast.AST
    """The ``@entrypoint(name=...)`` decorator node — anchored for suppression."""


@dataclass
class CodeEntrypointScan:
    """Accumulated results of scanning all Python files for ``@entrypoint`` decorations."""

    entrypoints: list[EntrypointLocation] = field(default_factory=list)
    app_classes: list[AppClassLocation] = field(default_factory=list)
    unresolved: list[UnresolvedLocation] = field(default_factory=list)

    def name_set(self) -> frozenset[str]:
        """Return the set of all wire names found in code."""
        return frozenset(ep.name for ep in self.entrypoints)


# ---------------------------------------------------------------------------
# Per-file scanner (called from scan_all)
# ---------------------------------------------------------------------------


def scan_file_for_entrypoints(
    tree: ast.Module,
    filename: str,
    result: CodeEntrypointScan,
) -> None:
    """Scan one parsed AST module, appending findings to *result* in-place.

    This function is a no-op when the file contains no ``application_sdk``
    imports (import-provenance guard).
    """
    ep_aliases = _sdk_entrypoint_aliases(tree)
    app_aliases = _sdk_app_aliases(tree)

    if not ep_aliases and not app_aliases:
        return  # No SDK imports in this file — skip entirely.

    for node in ast.walk(tree):
        # ── App subclass detection ────────────────────────────────────────────
        if app_aliases and isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name: str | None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                else:
                    base_name = None
                if base_name in app_aliases:
                    result.app_classes.append(
                        AppClassLocation(filename=filename, node=node)
                    )
                    break

        # ── @entrypoint-decorated method detection ────────────────────────────
        if not ep_aliases:
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for deco in node.decorator_list:
            is_ep_deco = (isinstance(deco, ast.Name) and deco.id in ep_aliases) or (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Name)
                and deco.func.id in ep_aliases
            )
            if not is_ep_deco:
                continue

            ep_name, is_unresolved = _extract_ep_name(deco, node.name)
            if is_unresolved:
                # Anchor to the decorator node so a comment-only suppress directive
                # on the line directly above the @entrypoint is picked up by make_finding.
                result.unresolved.append(
                    UnresolvedLocation(filename=filename, node=deco)
                )
            elif ep_name is not None:
                result.entrypoints.append(
                    EntrypointLocation(name=ep_name, filename=filename, node=deco)
                )
            break  # Only the first @entrypoint decorator on a method counts.
