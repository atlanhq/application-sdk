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
import re
from dataclasses import dataclass, field
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, _parse_directives

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


def _class_attribute_value(class_node: ast.ClassDef, attr: str) -> ast.expr | None:
    """The assigned expression of a class-body attribute, or ``None`` if unset.

    Covers both ``attr = ...`` and the annotated ``attr: T = ...`` form. A bare
    annotation binds no value, so the scan keeps walking: ``attr: ClassVar[str]``
    followed by ``attr = "..."`` is one attribute with a value, and stopping at
    the annotation would read a real declaration as absent.
    """
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == attr
            for target in stmt.targets
        ):
            return stmt.value
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == attr
            and stmt.value is not None
        ):
            return stmt.value
    return None


def _pascal_to_kebab(name: str) -> str:
    """``'MyApp'`` → ``'my-app'``, mirroring ``application_sdk.app.base``.

    Kept character-for-character equivalent to the SDK's own helper: this value
    is compared against the app name the toolkit baked into the generated
    manifest, so a looser transform (plain underscore replacement, say) reads a
    nameless ``App`` subclass as a *different* app and silently drops it from
    every identity-scoped comparison.
    """
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    spaced = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", spaced)
    return spaced.lower()


def _extract_app_name(class_node: ast.ClassDef) -> str | None:
    """The app name this class registers under, when statically knowable.

    Mirrors the SDK's derivation: a literal ``name = "..."`` wins; an absent
    attribute falls back to the kebab-cased class name. A non-literal
    assignment returns ``None`` — no identity claim can be made.
    """
    value = _class_attribute_value(class_node, "name")
    if value is None:
        return _pascal_to_kebab(class_node.name)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value or _pascal_to_kebab(class_node.name)
    return None


def _extract_legacy_aliases(
    class_node: ast.ClassDef,
) -> tuple[dict[str, str], bool]:
    """Parse a class body for a literal ``legacy_workflow_types`` declaration.

    Returns ``(aliases, is_unresolved)``: the ``{alias: entry-point name}``
    pairs when the assignment is a dict literal of string constants, or
    ``is_unresolved=True`` when the attribute is assigned something the scan
    cannot statically read (a variable, a comprehension, …).
    """
    value = _class_attribute_value(class_node, "legacy_workflow_types")
    if value is None:
        return {}, False
    if not isinstance(value, ast.Dict):
        return {}, True
    aliases: dict[str, str] = {}
    for key, val in zip(value.keys, value.values):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(val, ast.Constant)
            and isinstance(val.value, str)
        ):
            aliases[key.value] = val.value
        else:
            return {}, True
    return aliases, False


def _extract_removal_version(class_node: ast.ClassDef) -> tuple[str, bool]:
    """Parse a class body for a literal ``legacy_workflow_types_removal_version``.

    Returns ``(version, is_unresolved)``. An absent attribute reads as ``""``,
    the SDK default meaning "no expiry declared"; a non-literal assignment sets
    ``is_unresolved`` so K015 reports that it cannot compare rather than
    reporting a mismatch it cannot prove.
    """
    value = _class_attribute_value(class_node, "legacy_workflow_types_removal_version")
    if value is None:
        return "", False
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value, False
    return "", True


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

    app_name: str | None = None
    """The app's registered name: the literal ``name = "..."`` class attribute,
    or the kebab-cased class name when the attribute is absent (mirroring the
    SDK's derivation). ``None`` when the attribute exists but is not a string
    literal, so no identity claim can be made statically."""

    legacy_aliases: dict[str, str] = field(default_factory=dict)
    """This class's literal ``legacy_workflow_types`` pairs:
    ``{alias: entry-point name}``. Scoped per class — pooling declarations
    across App classes would let one app's declaration route another's
    entry point."""

    legacy_removal_version: str = ""
    """This class's literal ``legacy_workflow_types_removal_version``, or ``""``
    when the attribute is absent (the SDK default: no expiry). K015 compares it
    against the manifest block's ``removal_version``."""


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
    unresolved_aliases: list[AppClassLocation] = field(default_factory=list)
    """App subclasses whose ``legacy_workflow_types`` or
    ``legacy_workflow_types_removal_version`` assignment the scan cannot
    statically read (a variable, a comprehension, …), so K015 cannot compare the
    declaration against the manifest block."""

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
                    aliases, is_unresolved = _extract_legacy_aliases(node)
                    removal_version, version_unresolved = _extract_removal_version(node)
                    location = AppClassLocation(
                        filename=filename,
                        node=node,
                        app_name=_extract_app_name(node),
                        legacy_aliases={} if is_unresolved else aliases,
                        legacy_removal_version=removal_version,
                    )
                    result.app_classes.append(location)
                    if is_unresolved or version_unresolved:
                        result.unresolved_aliases.append(location)
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
                    EntrypointLocation(
                        name=ep_name,
                        filename=filename,
                        node=deco,
                    )
                )
            break  # Only the first @entrypoint decorator on a method counts.


def scan_paths_for_entrypoints(
    paths: list[Path], root: Path
) -> tuple[CodeEntrypointScan, dict[str, dict[int, _IgnoreDirective]]]:
    """Scan every path for ``@entrypoint`` data and inline suppression directives.

    Shared by P016 and K015 so the two read the same App-class facts from the
    same files; a second copy of this loop would be free to drift from P016's.
    Unreadable and unparseable files are skipped — a check reports drift it can
    prove, and a file it cannot parse proves nothing.
    """
    code = CodeEntrypointScan()
    directives_by_file: dict[str, dict[int, _IgnoreDirective]] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        if not isinstance(tree, ast.Module):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        directives_by_file[rel] = _parse_directives(text)
        scan_file_for_entrypoints(tree, rel, code)

    return code, directives_by_file
